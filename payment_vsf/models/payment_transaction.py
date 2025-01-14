# -*- coding: utf-8 -*-
# Copyright 2022 ODOOGAP/PROMPTEQUATION LDA
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

import logging
import re
import unicodedata

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import consteq, format_amount, ustr
from odoo.tools.misc import hmac as hmac_tool

from odoo.addons.payment_vsf import utils as payment_utils

_logger = logging.getLogger(__name__)


class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    company_id = fields.Many2one(  # Indexed to speed-up ORM searches (from ir_rule or others)
        related='acquirer_id.company_id', store=True, index=True)
    payment_token_id = fields.Many2one('payment.token', 'Payment Token', readonly=True,
                                       domain="[('acquirer_id', '=', acquirer_id)]")
    last_state_change = fields.Datetime(
        string="Last State Change Date", readonly=True, default=fields.Datetime.now)

    # Fields used for traceability
    operation = fields.Selection(  # This should not be trusted if the state is 'draft' or 'pending'
        string="Operation",
        selection=[
            ('online_redirect', "Online payment with redirection"),
            ('online_direct', "Online direct payment"),
            ('online_token', "Online payment by token"),
            ('validation', "Validation of the payment method"),
            ('offline', "Offline payment by token"),
            ('refund', "Refund")
        ],
        readonly=True,
        index=True,
    )
    source_transaction_id = fields.Many2one(
        string="Source Transaction",
        comodel_name='payment.transaction',
        help="The source transaction of related refund transactions",
        readonly=True
    )
    refunds_count = fields.Integer(string="Refunds Count", compute='_compute_refunds_count')
    invoices_count = fields.Integer(string="Invoices Count", compute='_compute_invoices_count')

    # Fields used for user redirection & payment post-processing
    tokenize = fields.Boolean(
        string="Create Token",
        help="Whether a payment token should be created when post-processing the transaction")
    landing_route = fields.Char(
        string="Landing Route",
        help="The route the user is redirected to after the transaction")
    callback_is_done = fields.Boolean(
        string="Callback Done", help="Whether the callback has already been executed",
        groups="base.group_system", readonly=True)

    # Duplicated partner values allowing to keep a record of them, should they be later updated
    partner_state_id = fields.Many2one(string="State", comodel_name='res.country.state')

    #=== COMPUTE METHODS ===#

    @api.depends('invoice_ids')
    def _compute_invoices_count(self):
        self.env.cr.execute(
            '''
            SELECT transaction_id, count(invoice_id)
            FROM account_invoice_transaction_rel
            WHERE transaction_id IN %s
            GROUP BY transaction_id
            ''',
            [tuple(self.ids)]
        )
        tx_data = dict(self.env.cr.fetchall())  # {id: count}
        for tx in self:
            tx.invoices_count = tx_data.get(tx.id, 0)

    def _compute_refunds_count(self):
        rg_data = self.env['payment.transaction'].read_group(
            domain=[('source_transaction_id', 'in', self.ids), ('operation', '=', 'refund')],
            fields=['source_transaction_id'],
            groupby=['source_transaction_id'],
        )
        data = {x['source_transaction_id'][0]: x['source_transaction_id_count'] for x in rg_data}
        for record in self:
            record.refunds_count = data.get(record.id, 0)

    #=== ACTION METHODS ===#

    def action_view_refunds(self):
        """ Return the action for the views of the refund transactions linked to the transaction.

        Note: self.ensure_one()

        :return: The action
        :rtype: dict
        """
        self.ensure_one()

        action = {
            'name': _("Refund"),
            'res_model': 'payment.transaction',
            'type': 'ir.actions.act_window',
        }
        if self.refunds_count == 1:
            refund_tx = self.env['payment.transaction'].search([
                ('source_transaction_id', '=', self.id),
            ])[0]
            action['res_id'] = refund_tx.id
            action['view_mode'] = 'form'
        else:
            action['view_mode'] = 'tree,form'
            action['domain'] = [('source_transaction_id', '=', self.id)]
        return action

    def action_capture(self):
        """ Check the state of the transactions and request their capture. """
        if any(tx.state != 'authorized' for tx in self):
            raise ValidationError(_("Only authorized transactions can be captured."))

        payment_utils.check_rights_on_recordset(self)
        for tx in self:
            if tx.provider == 'adyen_direct':
                # In sudo mode because we need to be able to read on acquirer fields.
                tx.sudo()._send_capture_request()
            else:
                tx.s2s_capture_transaction()

    def action_void(self):
        """ Check the state of the transaction and request to have them voided. """
        if any(tx.state != 'authorized' for tx in self):
            raise ValidationError(_("Only authorized transactions can be voided."))

        payment_utils.check_rights_on_recordset(self)
        for tx in self:
            if tx.provider == 'adyen_direct':
                # In sudo mode because we need to be able to read on acquirer fields.
                tx.sudo()._send_void_request()
            else:
                tx.sudo().s2s_void_transaction()

    def action_refund(self, amount_to_refund=None):
        """ Check the state of the transactions and request their refund.

        :param float amount_to_refund: The amount to be refunded
        :return: None
        """
        if any(tx.state != 'done' for tx in self):
            raise ValidationError(_("Only confirmed transactions can be refunded."))

        for tx in self:
            if tx.provider == 'adyen_direct':
                tx._send_refund_request(amount_to_refund)
            else:
                tx.s2s_do_refund()

    #=== BUSINESS METHODS - PAYMENT FLOW ===#

    @api.model
    def _compute_reference_changed(self, provider, prefix=None, separator='-', **kwargs):
        """ Compute a unique reference for the transaction.

        The reference either corresponds to the prefix if no other transaction with that prefix
        already exists, or follows the pattern `{computed_prefix}{separator}{sequence_number}` where
          - {computed_prefix} is:
            - The provided custom prefix, if any.
            - The computation result of `_compute_reference_prefix_changed` if the custom prefix is not
              filled but the kwargs are.
            - 'tx-{datetime}', if neither the custom prefix nor the kwargs are filled.
          - {separator} is a custom string also used in `_compute_reference_prefix_changed`.
          - {sequence_number} is the next integer in the sequence of references sharing the exact
            same prefix, '1' if there is only one matching reference (hence without sequence number)

        Examples:
          - Given the custom prefix 'example' which has no match with an existing reference, the
            full reference will be 'example'.
          - Given the custom prefix 'example' which matches the existing reference 'example', and
            the custom separator '-', the full reference will be 'example-1'.
          - Given the kwargs {'invoice_ids': [1, 2]}, the custom separator '-' and no custom prefix,
            the full reference will be 'INV1-INV2' (or similar) if no existing reference has the
            same prefix, or 'INV1-INV2-n' if n existing references have the same prefix.

        :param str provider: The provider of the acquirer handling the transaction
        :param str prefix: The custom prefix used to compute the full reference
        :param str separator: The custom separator used to separate the prefix from the suffix, and
                              passed to `_compute_reference_prefix_changed` if it is called
        :param dict kwargs: Optional values passed to `_compute_reference_prefix_changed` if no custom
                            prefix is provided
        :return: The unique reference for the transaction
        :rtype: str
        """
        # Compute the prefix
        if prefix:
            # Replace special characters by their ASCII alternative (é -> e ; ä -> a ; ...)
            prefix = unicodedata.normalize('NFKD', prefix).encode('ascii', 'ignore').decode('utf-8')
        if not prefix:  # Prefix not provided or voided above, compute it based on the kwargs
            prefix = self.sudo()._compute_reference_prefix_changed(provider, separator, **kwargs)
        if not prefix:  # Prefix not computed from the kwargs, fallback on time-based value
            prefix = payment_utils.singularize_reference_prefix()

        # Compute the sequence number
        reference = prefix  # The first reference of a sequence has no sequence number
        if self.sudo().search([('reference', '=', prefix)]):  # The reference already has a match
            # We now execute a second search on `payment.transaction` to fetch all the references
            # starting with the given prefix. The load of these two searches is mitigated by the
            # index on `reference`. Although not ideal, this solution allows for quickly knowing
            # whether the sequence for a given prefix is already started or not, usually not. An SQL
            # query wouldn't help either as the selector is arbitrary and doing that would be an
            # open-door to SQL injections.
            same_prefix_references = self.sudo().search(
                [('reference', 'like', f'{prefix}{separator}%')]
            ).with_context(prefetch_fields=False).mapped('reference')

            # A final regex search is necessary to figure out the next sequence number. The previous
            # search could not rely on alphabetically sorting the reference to infer the largest
            # sequence number because both the prefix and the separator are arbitrary. A given
            # prefix could happen to be a substring of the reference from a different sequence.
            # For instance, the prefix 'example' is a valid match for the existing references
            # 'example', 'example-1' and 'example-ref', in that order. Trusting the order to infer
            # the sequence number would lead to a collision with 'example-1'.
            search_pattern = re.compile(rf'^{prefix}{separator}(\d+)$')
            max_sequence_number = 0  # If no match is found, start the sequence with this reference
            for existing_reference in same_prefix_references:
                search_result = re.search(search_pattern, existing_reference)
                if search_result:  # The reference has the same prefix and is from the same sequence
                    # Find the largest sequence number, if any
                    current_sequence = int(search_result.group(1))
                    if current_sequence > max_sequence_number:
                        max_sequence_number = current_sequence

            # Compute the full reference
            reference = f'{prefix}{separator}{max_sequence_number + 1}'
        return reference

    @api.model
    def _compute_reference_prefix_changed(self, provider, separator, **values):
        """ Compute the reference prefix from the transaction values.

        If the `values` parameter has an entry with 'invoice_ids' as key and a list of (4, id, O) or
        (6, 0, ids) X2M command as value, the prefix is computed based on the invoice name(s).
        Otherwise, an empty string is returned.

        Note: This method should be called in sudo mode to give access to documents (INV, SO, ...).

        :param str provider: The provider of the acquirer handling the transaction
        :param str separator: The custom separator used to separate data references
        :param dict values: The transaction values used to compute the reference prefix. It should
                            have the structure {'invoice_ids': [(X2M command), ...], ...}.
        :return: The computed reference prefix if invoice ids are found, an empty string otherwise
        :rtype: str
        """
        command_list = values.get('invoice_ids')
        if command_list:
            # Extract invoice id(s) from the X2M commands
            invoice_ids = self._fields['invoice_ids'].convert_to_cache(command_list, self)
            invoices = self.env['account.move'].browse(invoice_ids).exists()
            if len(invoices) == len(invoice_ids):  # All ids are valid
                return separator.join(invoices.mapped('name'))
        return ''

    @api.model
    def _generate_callback_hash_changed(self, callback_model_id, callback_res_id, callback_method):
        """ Return the hash for the callback on the transaction.

        :param int callback_model_id: The model on which the callback method is defined, as a
                                      `res.model` id
        :param int callback_res_id: The record on which the callback method must be called, as an id
                                    of the callback model
        :param str callback_method: The name of the callback method
        :return: The callback hash
        :rtype: str
        """
        if callback_model_id and callback_res_id and callback_method:
            model_name = self.env['ir.model'].sudo().browse(callback_model_id).model
            token = f'{model_name}|{callback_res_id}|{callback_method}'
            callback_hash = hmac_tool(self.env(su=True), 'generate_callback_hash', token)
            return callback_hash
        return None

    def _send_payment_request(self):
        """ Request the provider of the acquirer handling the transaction to execute the payment.

        For an acquirer to support tokenization, it must override this method and call it to log the
        'sent' message, then request a money transfer to its provider.

        Note: self.ensure_one()

        :return: None
        """
        self.ensure_one()
        self._log_sent_message()

    def _send_refund_request(self, amount_to_refund=None, create_refund_transaction=True):
        """ Request the provider of the acquirer handling the transaction to refund it.

        For an acquirer to support refunds, it must override this method and request a refund
        to its provider.

        Note: self.ensure_one()

        :param float amount_to_refund: The amount to be refunded
        :param bool create_refund_transaction: Whether a refund transaction should be created
        :return: The refund transaction if any
        :rtype: recordset of `payment.transaction`
        """
        self.ensure_one()

        if create_refund_transaction:
            refund_tx = self._create_refund_transaction(amount_to_refund=amount_to_refund)
            refund_tx._log_sent_message()
            return refund_tx
        else:
            return self.env['payment.transaction']

    def _send_capture_request(self):
        """ Request the provider of the acquirer handling the transaction to capture it.

        For an acquirer to support authorization, it must override this method and request a capture
        to its provider.

        Note: self.ensure_one()

        :return: None
        """
        self.ensure_one()

    def _send_void_request(self):
        """ Request the provider of the acquirer handling the transaction to void it.

        For an acquirer to support authorization, it must override this method and request the
        transaction to be voided to its provider.

        Note: self.ensure_one()

        :return: None
        """
        self.ensure_one()

    def _create_refund_transaction(self, amount_to_refund=None, **custom_create_values):
        """ Create a new transaction with operation 'refund' and link it to the current transaction.

        :param float amount_to_refund: The strictly positive amount to refund, in the same currency
                                       as the source transaction
        :return: The refund transaction
        :rtype: recordset of `payment.transaction`
        """
        self.ensure_one()

        return self.create({
            'acquirer_id': self.acquirer_id.id,
            'reference': self._compute_reference_changed(self.provider, prefix=f'R-{self.reference}'),
            'amount': -(amount_to_refund or self.amount),
            'currency_id': self.currency_id.id,
            'payment_token_id': self.payment_token_id.id,
            'operation': 'refund',
            'source_transaction_id': self.id,
            'partner_id': self.partner_id.id,
            **custom_create_values,
        })

    @api.model
    def _handle_feedback_data(self, provider, data):
        """ Match the transaction with the feedback data, update its state and return it.

        :param str provider: The provider of the acquirer that handled the transaction
        :param dict data: The feedback data sent by the provider
        :return: The transaction
        :rtype: recordset of `payment.transaction`
        """
        tx = self._get_tx_from_feedback_data(provider, data)
        tx._process_feedback_data(data)
        tx._execute_callback()
        return tx

    @api.model
    def _get_tx_from_feedback_data(self, provider, data):
        """ Find the transaction based on the feedback data.

        For an acquirer to handle transaction post-processing, it must overwrite this method and
        return the transaction matching the data.

        :param str provider: The provider of the acquirer that handled the transaction
        :param dict data: The feedback data sent by the acquirer
        :return: The transaction if found
        :rtype: recordset of `payment.transaction`
        """
        return self

    def _process_feedback_data(self, data):
        """ Update the transaction state and the acquirer reference based on the feedback data.

        For an acquirer to handle transaction post-processing, it must overwrite this method and
        process the feedback data.

        Note: self.ensure_one()

        :param dict data: The feedback data sent by the acquirer
        :return: None
        """
        self.ensure_one()

    def _set_pending(self, state_message=None):
        """ Update the transactions' state to 'pending'.

        :param str state_message: The reason for which the transaction is set in 'pending' state
        :return: None
        """
        allowed_states = ('draft',)
        target_state = 'pending'
        txs_to_process = self._update_state(allowed_states, target_state, state_message)
        txs_to_process._log_received_message()

    def _set_authorized(self, state_message=None):
        """ Update the transactions' state to 'authorized'.

        :param str state_message: The reason for which the transaction is set in 'authorized' state
        :return: None
        """
        allowed_states = ('draft', 'pending')
        target_state = 'authorized'
        txs_to_process = self._update_state(allowed_states, target_state, state_message)
        txs_to_process._log_received_message()

    def _set_done(self, state_message=None):
        """ Update the transactions' state to 'done'.

        :return: None
        """
        allowed_states = ('draft', 'pending', 'authorized', 'error')
        target_state = 'done'
        txs_to_process = self._update_state(allowed_states, target_state, state_message)
        txs_to_process._log_received_message()

    def _set_canceled(self, state_message=None):
        """ Update the transactions' state to 'cancel'.

        :param str state_message: The reason for which the transaction is set in 'cancel' state
        :return: None
        """
        allowed_states = ('draft', 'pending', 'authorized')
        target_state = 'cancel'
        txs_to_process = self._update_state(allowed_states, target_state, state_message)
        # Cancel the existing payments
        txs_to_process.mapped('payment_id').action_cancel()
        txs_to_process._log_received_message()

    def _set_error(self, state_message):
        """ Update the transactions' state to 'error'.

        :param str state_message: The reason for which the transaction is set in 'error' state
        :return: None
        """
        allowed_states = ('draft', 'pending', 'authorized')
        target_state = 'error'
        txs_to_process = self._update_state(allowed_states, target_state, state_message)
        txs_to_process._log_received_message()

    def _update_state(self, allowed_states, target_state, state_message):
        """ Update the transactions' state to the target state if the current state allows it.

        If the current state is the same as the target state, the transaction is skipped.

        :param tuple[str] allowed_states: The allowed source states for the target state
        :param str target_state: The target state
        :param str state_message: The message to set as `state_message`
        :return: The recordset of transactions whose state was correctly updated
        :rtype: recordset of `payment.transaction`
        """

        def _classify_by_state(_transactions):
            """Classify the transactions according to their current state.

            For each transaction of the current recordset, if:
                - The state is an allowed state: the transaction is flagged as 'to process'.
                - The state is equal to the target state: the transaction is flagged as 'processed'.
                - The state matches none of above: the transaction is flagged as 'in wrong state'.

            :param recordset _transactions: The transactions to classify, as a `payment.transaction`
                                            recordset
            :return: A 3-items tuple of recordsets of classified transactions, in this order:
                     transactions 'to process', 'processed', and 'in wrong state'
            :rtype: tuple(recordset)
            """
            _txs_to_process = _transactions.filtered(lambda _tx: _tx.state in allowed_states)
            _txs_already_processed = _transactions.filtered(lambda _tx: _tx.state == target_state)
            _txs_wrong_state = _transactions - _txs_to_process - _txs_already_processed

            return _txs_to_process, _txs_already_processed, _txs_wrong_state

        txs_to_process, txs_already_processed, txs_wrong_state = _classify_by_state(self)
        for tx in txs_already_processed:
            _logger.info(
                "tried to write tx state with same value (ref: %s, state: %s)",
                tx.reference, tx.state
            )
        for tx in txs_wrong_state:
            logging_values = {
                'reference': tx.reference,
                'tx_state': tx.state,
                'target_state': target_state,
                'allowed_states': allowed_states,
            }
            _logger.warning(
                "tried to write tx state with illegal value (ref: %(reference)s, previous state "
                "%(tx_state)s, target state: %(target_state)s, expected previous state to be in: "
                "%(allowed_states)s)", logging_values
            )
        txs_to_process.write({
            'state': target_state,
            'state_message': state_message,
            'date': fields.Datetime.now(),
            'last_state_change': fields.Datetime.now(),
        })
        return txs_to_process

    def _execute_callback(self):
        """ Execute the callbacks defined on the transactions.

        Callbacks that have already been executed are silently ignored. This case can happen when a
        transaction is first authorized before being confirmed, for instance. In this case, both
        status updates try to execute the callback.

        Only successful callbacks are marked as done. This allows callbacks to reschedule themselves
        should the conditions not be met in the present call.

        :return: None
        """
        for tx in self.filtered(lambda t: not t.sudo().callback_is_done):
            # Only use sudo to check, not to execute
            tx_sudo = tx.sudo()
            model_sudo = tx_sudo.callback_model_id
            res_id = tx_sudo.callback_res_id
            method = tx_sudo.callback_method
            callback_hash = tx_sudo.callback_hash
            if not (model_sudo and res_id and method):
                continue  # Skip transactions with unset (or not properly defined) callbacks

            valid_callback_hash = self._generate_callback_hash_changed(model_sudo.id, res_id, method)
            if not consteq(ustr(valid_callback_hash), callback_hash):
                _logger.warning("invalid callback signature for transaction with id %s", tx.id)
                continue  # Ignore tampered callbacks

            record = self.env[model_sudo.model].browse(res_id).exists()
            if not record:
                logging_values = {
                    'model': model_sudo.model,
                    'record_id': res_id,
                    'tx_id': tx.id,
                }
                _logger.warning(
                    "invalid callback record %(model)s.%(record_id)s for transaction with id "
                    "%(tx_id)s", logging_values
                )
                continue  # Ignore invalidated callbacks

            success = getattr(record, method)(tx)  # Execute the callback
            tx_sudo.callback_is_done = success or success is None  # Missing returns are successful

    #=== BUSINESS METHODS - LOGGING ===#

    def _log_sent_message(self):
        """ Log in the chatter of relevant documents that the transactions have been initiated.

        :return: None
        """
        for tx in self:
            message = tx._get_sent_message()
            tx._log_message_on_linked_documents(message)

    def _log_received_message(self):
        """ Log in the chatter of relevant documents that the transactions have been received.

        A transaction is 'received' when a response is received from the provider of the acquirer
        handling the transaction.

        :return: None
        """
        for tx in self:
            message = tx._get_received_message()
            tx._log_message_on_linked_documents(message)

    def _log_message_on_linked_documents(self, message):
        """ Log a message on the payment and the invoices linked to the transaction.

        For a module to implement payments and link documents to a transaction, it must override
        this method and call super, then log the message on documents linked to the transaction.

        Note: self.ensure_one()

        :param str message: The message to be logged
        :return: None
        """
        self.ensure_one()
        if self.source_transaction_id.payment_id:
            self.source_transaction_id.payment_id.message_post(body=message)
            for invoice in self.source_transaction_id.invoice_ids:
                invoice.message_post(body=message)
        for invoice in self.invoice_ids:
            invoice.message_post(body=message)

    #=== BUSINESS METHODS - GETTERS ===#

    def _get_sent_message(self):
        """ Return the message stating that the transaction has been requested.

        Note: self.ensure_one()

        :return: The 'transaction sent' message
        :rtype: str
        """
        self.ensure_one()

        # Choose the message based on the payment flow
        if self.operation in ('online_redirect', 'online_direct'):
            message = _(
                "A transaction with reference %(ref)s has been initiated (%(acq_name)s).",
                ref=self.reference, acq_name=self.acquirer_id.name
            )
        elif self.operation == 'refund':
            formatted_amount = format_amount(self.env, -self.amount, self.currency_id)
            message = _(
                "A refund request of %(amount)s has been sent. The payment will be created soon. "
                "Refund transaction reference: %(ref)s (%(acq_name)s).",
                amount=formatted_amount, ref=self.reference, acq_name=self.acquirer_id.name
            )
        else:  # 'online_token'
            message = _(
                "A transaction with reference %(ref)s has been initiated using the payment method "
                "%(token_name)s (%(acq_name)s).",
                ref=self.reference, token_name=self.payment_token_id.name, acq_name=self.acquirer_id.name
            )
        return message

    def _get_received_message(self):
        """ Return the message stating that the transaction has been received by the provider.

        Note: self.ensure_one()
        """
        self.ensure_one()

        formatted_amount = format_amount(self.env, self.amount, self.currency_id)
        if self.state == 'pending':
            message = _(
                "The transaction with reference %(ref)s for %(amount)s is pending (%(acq_name)s).",
                ref=self.reference, amount=formatted_amount, acq_name=self.acquirer_id.name
            )
        elif self.state == 'authorized':
            message = _(
                "The transaction with reference %(ref)s for %(amount)s has been authorized "
                "(%(acq_name)s).", ref=self.reference, amount=formatted_amount,
                acq_name=self.acquirer_id.name
            )
        elif self.state == 'done':
            message = _(
                "The transaction with reference %(ref)s for %(amount)s has been confirmed "
                "(%(acq_name)s).", ref=self.reference, amount=formatted_amount,
                acq_name=self.acquirer_id.name
            )
            if self.payment_id:
                message += "<br />" + _(
                    "The related payment is posted: %s",
                    self.payment_id._get_payment_chatter_link()
                )
        elif self.state == 'error':
            message = _(
                "The transaction with reference %(ref)s for %(amount)s encountered an error"
                " (%(acq_name)s).",
                ref=self.reference, amount=formatted_amount, acq_name=self.acquirer_id.name
            )
            if self.state_message:
                message += "<br />" + _("Error: %s", self.state_message)
        else:
            message = _(
                "The transaction with reference %(ref)s for %(amount)s is canceled (%(acq_name)s).",
                ref=self.reference, amount=formatted_amount, acq_name=self.acquirer_id.name
            )
            if self.state_message:
                message += "<br />" + _("Reason: %s", self.state_message)
        return message
