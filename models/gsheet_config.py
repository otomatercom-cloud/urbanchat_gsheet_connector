import csv
import io
import logging
from datetime import timedelta

import requests

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = 'https://oauth2.googleapis.com/token'
GOOGLE_SHEETS_API = 'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{range_}'


class UrbanchatGsheetColumnMap(models.Model):
    _name = 'urbanchat.gsheet.column.map'
    _description = 'Urban Chat Sheet — Column Mapping'
    _order = 'sequence, id'

    config_id = fields.Many2one('urbanchat.gsheet.config', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    sheet_header = fields.Char(
        string='Sheet Column Header', required=True,
        help="Must match the column header text in row 1 of the Google Sheet "
             "(case-insensitive, extra spaces ignored).")
    target_field = fields.Selection([
        ('name', 'Lead Name'),
        ('phone_number', 'Mobile (Primary)'),
        ('phone_number_second', 'Phone Number (Secondary)'),
        ('parent_number', 'Parent Number'),
        ('email_address', 'Email'),
        ('course_interested', 'Course Interested'),
        ('leads_source', 'Lead Source (matched by name)'),
        ('lead_owner_name', 'Lead Owner Name (matched to Admission Officer)'),
        ('skip', "Don't Import — Reference Only"),
    ], required=True, string='Maps To')


class UrbanchatGsheetConfig(models.Model):
    _name = 'urbanchat.gsheet.config'
    _description = 'Urban Chat — Google Sheet Lead Connector'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(string='Connector Name', required=True, default='Urban Chat Leads',
                        tracking=True)
    active = fields.Boolean(default=True)

    # ── Connection ──────────────────────────────────────────────────────────
    connection_type = fields.Selection([
        ('csv', 'Public CSV Link (Publish to Web)'),
        ('oauth', 'Google OAuth (Sheets API)'),
    ], required=True, default='csv', string='Connection Method', tracking=True)

    csv_url = fields.Char(
        string='Published CSV URL',
        help="File > Share > Publish to web > select the sheet/tab > CSV format. "
             "Paste the resulting link here. The sheet must be reachable without login.")

    oauth_client_id = fields.Char(string='OAuth Client ID')
    oauth_client_secret = fields.Char(string='OAuth Client Secret')
    oauth_refresh_token = fields.Char(
        string='OAuth Refresh Token',
        help="Generate this once via your Google Cloud OAuth consent flow "
             "(or the OAuth Playground) authorizing at least the "
             "'https://www.googleapis.com/auth/spreadsheets.readonly' scope, "
             "then paste the resulting refresh token here. Odoo exchanges it "
             "for a fresh access token on every sync — no further manual login needed.")
    spreadsheet_id = fields.Char(
        string='Spreadsheet ID',
        help="The long ID segment in the sheet's URL: "
             "docs.google.com/spreadsheets/d/<THIS PART>/edit")
    sheet_range = fields.Char(
        string='Sheet & Range', default='Sheet1!A:Z',
        help="e.g. 'Sheet1!A:Z' or 'Leads!A1:H'")

    # ── Column mapping ─────────────────────────────────────────────────────
    column_map_ids = fields.One2many(
        'urbanchat.gsheet.column.map', 'config_id', string='Column Mapping')

    # ── Duplicate handling ──────────────────────────────────────────────────
    duplicate_check_field = fields.Selection([
        ('phone_number', 'Mobile Number'),
        ('email_address', 'Email'),
    ], default='phone_number', required=True, string='Check Duplicate By')
    duplicate_action = fields.Selection([
        ('skip', 'Skip'),
        ('update', 'Update Existing'),
    ], default='skip', required=True, string='On Duplicate')

    default_source_id = fields.Many2one(
        'leads.sources', string='Default Lead Source',
        help="Used when the sheet has no source column mapped, or the value "
             "in it doesn't match any existing Lead Source by name. "
             "leads_source is required on every lead, so if this is empty "
             "and no match is found, that row is logged as an error.")
    default_quality = fields.Selection(
        selection='_selection_lead_quality', string='Default Quality', default='new')

    @api.model
    def _selection_lead_quality(self):
        return self.env['leads.logic']._fields['lead_quality'].selection

    # ── Owner matching + round-robin fallback ───────────────────────────────
    default_team_ids = fields.Many2many(
        'lead.team', string='Fallback Team(s) (Round Robin)',
        help="If the sheet's 'Lead Owner Name' cell doesn't match any "
             "Admission Officer by name, the lead is instead handed out in "
             "round-robin rotation across these team(s) — one lead each, in "
             "turn — using the same rotation counter shared with the "
             "automatic assignment engine and Lead Import. With more than "
             "one team selected, leads also cycle across the teams "
             "themselves. Leave empty to leave unmatched leads unassigned.")
    include_team_leads_in_fallback = fields.Boolean(
        string='Include Team Leads in Fallback Rotation', default=False)

    # ── Sync scheduling / status ────────────────────────────────────────────
    sync_interval_minutes = fields.Integer(
        string='Auto-Sync Every (minutes)', default=15,
        help="The background scheduler checks every few minutes and only "
             "actually syncs this connector once this many minutes have "
             "passed since its last sync.")
    last_sync_date = fields.Datetime(string='Last Synced', readonly=True, copy=False)
    last_sync_summary = fields.Char(string='Last Sync Result', readonly=True, copy=False)

    sync_log_ids = fields.One2many('urbanchat.sheet.sync.log', 'config_id', string='Sync Log')
    sync_log_count = fields.Integer(compute='_compute_sync_log_count')

    def _compute_sync_log_count(self):
        for rec in self:
            rec.sync_log_count = self.env['urbanchat.sheet.sync.log'].search_count(
                [('config_id', '=', rec.id)])

    def action_view_sync_log(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'urbanchat_gsheet_connector.action_urbanchat_sheet_sync_log')
        action['domain'] = [('config_id', '=', self.id)]
        action['context'] = {'default_config_id': self.id}
        return action

    # ── Team round-robin helpers (same pattern as lead_import_19) ───────────
    def _team_at_cycle_index(self, idx):
        """Return the team at position `idx` in default_team_ids, wrapping
        around. Caller owns the counter as a plain local variable — Odoo
        recordsets don't reliably support stashing scratch state on self."""
        teams = self.default_team_ids
        if not teams:
            return False
        return teams[idx % len(teams)]

    def _pick_team_round_robin(self, team):
        """Return the next hr.employee for `team` using the SAME round-robin
        counter table (lead.assignment.counter, keyed by team_id) that
        custom_leads_19's lead.assignment.rule._round_robin_member() uses,
        so this connector's fallback assignments and the main assignment
        engine share one fair rotation per team."""
        if not team:
            return False
        rule = self.env['lead.assignment.rule'].new({
            'include_team_leads': self.include_team_leads_in_fallback,
        })
        return rule._round_robin_member(team)

    def _match_employee_by_name(self, raw_name):
        """Case-insensitive, whitespace-trimmed exact match of the sheet's
        Lead Owner Name against hr.employee.name, scoped to employees who
        are actually members of a lead.team (i.e. known Admission Officers),
        rather than any employee in the company."""
        raw_name = (raw_name or '').strip()
        if not raw_name:
            return False
        members = self.env['lead.team.member'].search([])
        target = raw_name.lower()
        for member in members:
            emp_name = (member.employee_id.name or '').strip()
            if emp_name and emp_name.lower() == target:
                return member.employee_id
        return False

    # ── Fetching the sheet ───────────────────────────────────────────────────
    def _get_oauth_access_token(self):
        self.ensure_one()
        if not (self.oauth_client_id and self.oauth_client_secret and self.oauth_refresh_token):
            raise UserError(_(
                "OAuth Client ID, Client Secret and Refresh Token are all "
                "required for OAuth sync."))
        try:
            resp = requests.post(GOOGLE_TOKEN_URL, data={
                'client_id': self.oauth_client_id,
                'client_secret': self.oauth_client_secret,
                'refresh_token': self.oauth_refresh_token,
                'grant_type': 'refresh_token',
            }, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise UserError(_("Could not refresh the Google OAuth token: %s") % e)
        data = resp.json()
        token = data.get('access_token')
        if not token:
            raise UserError(_("Google did not return an access token: %s") % data)
        return token

    def _fetch_rows(self):
        """Returns a list of rows (each a list of cell strings), row 0 = header."""
        self.ensure_one()
        if self.connection_type == 'csv':
            if not self.csv_url:
                raise UserError(_("Set the Published CSV URL first."))
            try:
                resp = requests.get(self.csv_url, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as e:
                raise UserError(_("Could not fetch the CSV sheet: %s") % e)
            content = resp.content.decode('utf-8-sig', errors='replace')
            return list(csv.reader(io.StringIO(content)))
        else:  # oauth
            if not (self.spreadsheet_id and self.sheet_range):
                raise UserError(_("Set the Spreadsheet ID and Sheet & Range first."))
            token = self._get_oauth_access_token()
            url = GOOGLE_SHEETS_API.format(
                spreadsheet_id=self.spreadsheet_id, range_=self.sheet_range)
            try:
                resp = requests.get(
                    url, headers={'Authorization': 'Bearer %s' % token},
                    params={'majorDimension': 'ROWS'}, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as e:
                raise UserError(_("Could not fetch the sheet via Google Sheets API: %s") % e)
            return resp.json().get('values', [])

    # ── Core sync ────────────────────────────────────────────────────────────
    def _sync(self):
        self.ensure_one()
        if not self.column_map_ids:
            raise UserError(_("Configure the Column Mapping before syncing."))

        rows = self._fetch_rows()
        result = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': 0}
        if not rows:
            self.write({
                'last_sync_date': fields.Datetime.now(),
                'last_sync_summary': _("No rows returned from the sheet."),
            })
            return result

        header = [(h or '').strip().lower() for h in rows[0]]
        data_rows = rows[1:]

        header_to_map = {}
        for line in self.column_map_ids:
            key = (line.sheet_header or '').strip().lower()
            if key in header:
                header_to_map[header.index(key)] = line

        if not header_to_map:
            raise UserError(_(
                "None of the configured column headers were found in row 1 "
                "of the sheet. Row 1 was: %s") % ', '.join(rows[0]))

        Leads = self.env['leads.logic']
        Log = self.env['urbanchat.sheet.sync.log']
        team_cycle_idx = 0

        for row_num, row in enumerate(data_rows, start=2):  # row 1 = header
            if not any((c or '').strip() for c in row):
                continue  # fully blank row
            try:
                vals = {}
                owner_name_raw = False

                for col_idx, line in header_to_map.items():
                    cell = row[col_idx].strip() if col_idx < len(row) and row[col_idx] else ''
                    if not cell or line.target_field == 'skip':
                        continue
                    if line.target_field == 'lead_owner_name':
                        owner_name_raw = cell
                    elif line.target_field == 'leads_source':
                        source = self.env['leads.sources'].search(
                            [('name', '=ilike', cell)], limit=1)
                        if source:
                            vals['leads_source'] = source.id
                    else:
                        vals[line.target_field] = cell

                if not vals.get('name') and not vals.get('phone_number'):
                    continue  # not enough to make a lead out of

                if 'leads_source' not in vals and self.default_source_id:
                    vals['leads_source'] = self.default_source_id.id
                if 'leads_source' not in vals:
                    result['errors'] += 1
                    Log.create({
                        'config_id': self.id, 'row_number': row_num, 'status': 'error',
                        'raw_owner_name': owner_name_raw,
                        'message': _("No Lead Source resolved for this row "
                                     "(sheet value didn't match, and no Default "
                                     "Lead Source is set)."),
                    })
                    continue

                vals.setdefault('lead_quality', self.default_quality or 'new')
                vals.setdefault('name', vals.get('phone_number') or 'Urban Chat Lead')

                dup_field = self.duplicate_check_field
                existing = False
                if vals.get(dup_field):
                    existing = Leads.search([(dup_field, '=', vals[dup_field])], limit=1)

                # ── Resolve owner: name match first, round-robin fallback second ──
                matched_employee = self._match_employee_by_name(owner_name_raw)
                fallback_team = False
                if matched_employee:
                    vals['lead_owner'] = matched_employee.id
                elif self.default_team_ids and not (existing and self.duplicate_action == 'skip'):
                    fallback_team = self._team_at_cycle_index(team_cycle_idx)
                    team_cycle_idx += 1
                    rr_employee = self._pick_team_round_robin(fallback_team)
                    if rr_employee:
                        vals['lead_owner'] = rr_employee.id

                if existing:
                    if self.duplicate_action == 'skip':
                        result['skipped'] += 1
                        Log.create({
                            'config_id': self.id, 'row_number': row_num, 'lead_id': existing.id,
                            'status': 'skipped', 'raw_owner_name': owner_name_raw,
                            'message': _("Duplicate by %s — skipped.") % dup_field,
                        })
                        continue
                    existing.write(vals)
                    result['updated'] += 1
                    Log.create({
                        'config_id': self.id, 'row_number': row_num, 'lead_id': existing.id,
                        'status': 'updated', 'raw_owner_name': owner_name_raw,
                        'matched_employee_id': matched_employee.id if matched_employee else False,
                        'fallback_team_id': fallback_team.id if fallback_team else False,
                    })
                    continue

                lead = Leads.create(vals)
                result['created'] += 1
                Log.create({
                    'config_id': self.id, 'row_number': row_num, 'lead_id': lead.id,
                    'status': 'created', 'raw_owner_name': owner_name_raw,
                    'matched_employee_id': matched_employee.id if matched_employee else False,
                    'fallback_team_id': fallback_team.id if fallback_team else False,
                })

                if fallback_team and lead:
                    self.env['lead.assignment.history'].sudo().create({
                        'lead_id': lead.id,
                        'owner_id': vals.get('lead_owner'),
                        'assigned_team_id': fallback_team.id,
                        'assignment_rule_id': False,
                        'assignment_type': 'manual',
                        'new_team_id': fallback_team.id,
                        'new_owner_id': vals.get('lead_owner'),
                        'changed_by': self.env.uid,
                        'assigned_date': fields.Datetime.now(),
                        'assigned_by': self.env.uid,
                    })

            except Exception as e:  # noqa: BLE001 — keep syncing remaining rows
                result['errors'] += 1
                _logger.exception(
                    "urbanchat.gsheet.config: sync error on sheet row %s (config %s)",
                    row_num, self.name)
                Log.create({
                    'config_id': self.id, 'row_number': row_num, 'status': 'error',
                    'message': str(e)[:500],
                })

        summary = _("Created %(c)s, Updated %(u)s, Skipped %(s)s, Errors %(e)s") % {
            'c': result['created'], 'u': result['updated'],
            's': result['skipped'], 'e': result['errors'],
        }
        self.write({
            'last_sync_date': fields.Datetime.now(),
            'last_sync_summary': summary,
        })
        return result

    # ── Public actions ───────────────────────────────────────────────────────
    def action_sync_now(self):
        self.ensure_one()
        result = self._sync()
        message = _(
            "Created: %(c)s   Updated: %(u)s   Skipped: %(s)s   Errors: %(e)s"
        ) % {
            'c': result['created'], 'u': result['updated'],
            's': result['skipped'], 'e': result['errors'],
        }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Urban Chat Sync Complete'),
                'message': message,
                'type': 'warning' if result['errors'] else 'success',
                'sticky': bool(result['errors']),
            },
        }

    @api.model
    def cron_sync_all(self):
        """Called every few minutes by the scheduled action. Each connector
        only actually syncs once its own sync_interval_minutes has elapsed
        since its last sync, so different connectors can run on different
        cadences off of a single cron."""
        now = fields.Datetime.now()
        configs = self.search([('active', '=', True)])
        for config in configs:
            if config.last_sync_date:
                due_at = config.last_sync_date + timedelta(
                    minutes=config.sync_interval_minutes or 15)
                if now < due_at:
                    continue
            try:
                config._sync()
                self.env.cr.commit()
            except Exception:
                _logger.exception(
                    "urbanchat.gsheet.config: scheduled sync failed for %s", config.name)
                self.env.cr.rollback()
