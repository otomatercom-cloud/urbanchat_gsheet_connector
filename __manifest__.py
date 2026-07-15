{
    'name': 'Urban Chat — Google Sheet Lead Connector',
    'version': '19.0.1.1.1',
    'summary': 'Pulls leads from a Google Sheet (public CSV or OAuth) and auto-assigns '
               'by matching the sheet\'s Lead Owner Name to an Admission Officer, '
               'with round-robin fallback across a chosen team.',
    'author': 'Otomater',
    'category': 'CRM',
    'depends': ['custom_leads_19'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/gsheet_config_views.xml',
        'views/sync_log_views.xml',
        'views/menu.xml',
    ],
    'external_dependencies': {
        'python': ['requests'],
    },
    'installable': True,
    'auto_install': False,
    'license': 'LGPL-3',
}
