# -*- coding: utf-8 -*-
# Copyright 2022 ODOOGAP/PROMPTEQUATION LDA
# License LGPL-3.0 or later (http://www.gnu.org/licenses/lgpl).

{
    'name': 'VSF Payment Acquirer',
    'version': '1.0',
    'category': 'Hidden',
    'summary': 'Base Module To VSF Payment Acquirers',
    'description': """VSF Payment Acquirer Base Module""",
    'depends': [
        'payment',
        'website_payment',
    ],
    'data': [
        'views/payment_views.xml',
    ],
    'auto_install': True,
    'license': 'LGPL-3',
}
