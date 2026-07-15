from odoo import models, fields


class UrbanchatSheetSyncLog(models.Model):
    _name = 'urbanchat.sheet.sync.log'
    _description = 'Urban Chat Sheet — Sync Log'
    _order = 'id desc'

    config_id = fields.Many2one(
        'urbanchat.gsheet.config', string='Connector', required=True,
        ondelete='cascade', index=True)
    sync_date = fields.Datetime(default=fields.Datetime.now, readonly=True)
    row_number = fields.Integer(string='Sheet Row #',
                                 help="Row number in the Google Sheet (row 1 is the header).")
    lead_id = fields.Many2one('leads.logic', string='Lead')
    status = fields.Selection([
        ('created', 'Created'),
        ('updated', 'Updated (Duplicate)'),
        ('skipped', 'Skipped (Duplicate)'),
        ('error', 'Error'),
    ], required=True, string='Status')
    raw_owner_name = fields.Char(string='Sheet "Lead Owner Name"')
    matched_employee_id = fields.Many2one('hr.employee', string='Matched Admission Officer')
    fallback_team_id = fields.Many2one(
        'lead.team', string='Round-Robin Team Used',
        help="Set when the owner name in the sheet didn't match any Admission "
             "Officer, so the lead was instead handed to the next person in "
             "this team's rotation.")
    message = fields.Char(string='Note / Error')
