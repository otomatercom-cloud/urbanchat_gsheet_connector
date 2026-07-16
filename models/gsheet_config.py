import csv
import io
import re
import logging
from datetime import timedelta

import requests

from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError

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
        ('call_response', 'Response (lead "Response" text field)'),
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
        ('reattempt', 'Send to Re-Attempt'),
    ], default='skip', required=True, string='On Duplicate',
        help="Skip: ignore the row. Update Existing: overwrite the existing "
             "lead's fields with the sheet values. Send to Re-Attempt: keep "
             "the existing lead untouched and create an otomater.lead.reattempt "
             "request (Pending Review) for the Team Lead to approve, exactly "
             "like a duplicate entered manually.")

    auto_create_source = fields.Boolean(
        string='Auto-Create Missing Sources', default=True,
        help="When the sheet's source column has a value that doesn't match "
             "any existing Lead Source by name, create it automatically "
             "(case-insensitive matching, so no duplicate sources are made). "
             "If off, unmatched values fall back to the Default Lead Source.")

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
    owner_match_mode = fields.Selection([
        ('exact', 'Exact Match'),
        ('contains', 'Exact, then Name Contains'),
    ], default='exact', required=True, string='Officer Name Matching',
        help="Exact Match: the sheet's Lead Owner Name must equal the "
             "Admission Officer's name (case/space-insensitive).\n"
             "Exact, then Name Contains: if no exact match, also matches when "
             "the sheet value is contained in the officer's name or vice "
             "versa (e.g. sheet 'Anjali' matches officer 'Anjali Krishnan'). "
             "If several officers match this way, the row is treated as "
             "unmatched (round-robin fallback) and the ambiguity is logged.")
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

    owner_alias_ids = fields.One2many(
        'urbanchat.owner.alias', 'config_id', string='Owner Name Aliases',
        help="Alternate spellings agents use in the sheet, mapped to the "
             "right Admission Officer — e.g. 'FOUSIYA LATHEEF' -> "
             "FOUSIYA T Y. Aliases are checked BEFORE exact/contains "
             "matching.")
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

    @api.model
    def _normalize_person_name(self, name):
        """Lowercase, strip punctuation (dots, commas, hyphens...), collapse
        whitespace — so 'SAJINA. A', 'Sajina A' and 'sajina.a' all compare
        equal."""
        name = (name or '').lower()
        name = re.sub(r'[^a-z0-9]+', ' ', name)
        return ' '.join(name.split())

    def _match_employee_by_name(self, raw_name):
        """Match the sheet's Lead Owner Name against Admission Officers
        (hr.employee records that are members of a lead.team).

        Returns (employee_or_False, note_or_'').

        Names are compared punctuation-insensitively ('SAJINA. A' ==
        'SAJINA A').

        Pass 1 — always: normalized EXACT match.
        Pass 2 — only when owner_match_mode == 'contains' and pass 1 found
        nothing: substring match in either direction (sheet value inside the
        officer's name, or the officer's name inside the sheet value).
        If pass 2 matches more than one distinct officer, that's ambiguous:
        no match is returned and the note says who collided, so the lead
        falls through to the round-robin fallback instead of guessing.
        """
        target = self._normalize_person_name(raw_name)
        if not target:
            return False, ''

        # Pass 0 — explicit aliases configured on this connector win over
        # everything (normalized comparison, so punctuation/case-proof).
        for alias in self.owner_alias_ids:
            if self._normalize_person_name(alias.alias) == target \
                    and alias.employee_id:
                return alias.employee_id, _(
                    "Matched via alias: '%s' → %s") % (
                        raw_name, alias.employee_id.name)

        # Candidate pool = Admission Officers (team members) AND Team Leads —
        # both can be named as the lead owner in the sheet.
        candidates = {}  # employee_id -> (employee, normalized name)
        for member in self.env['lead.team.member'].search([]):
            emp = member.employee_id
            if emp:
                candidates[emp.id] = (emp, self._normalize_person_name(emp.name))
        for team in self.env['lead.team'].search([]):
            for emp in team.team_lead_ids:
                candidates[emp.id] = (emp, self._normalize_person_name(emp.name))

        for emp, emp_name in candidates.values():
            if emp_name and emp_name == target:
                return emp, ''

        if self.owner_match_mode != 'contains':
            return False, ''

        hits = []
        for emp, emp_name in candidates.values():
            if emp_name and (target in emp_name or emp_name in target):
                hits.append(emp)
        if len(hits) == 1:
            return hits[0], _("Matched by name-contains: '%s' ~ '%s'") % (
                raw_name, hits[0].name)
        if len(hits) > 1:
            names = ', '.join(e.name for e in hits[:5])
            return False, _(
                "Ambiguous owner name '%s' — contains-matched %s officers "
                "(%s). Used round-robin fallback instead."
            ) % (raw_name, len(hits), names)
        return False, ''

    # ── Source resolution (match or auto-create, no duplicates) ─────────────
    def _resolve_source(self, cell, source_cache):
        """Return a leads.sources id for the sheet cell value.

        Matching is case-insensitive on the trimmed value. If nothing
        matches and auto_create_source is on, the source is created once —
        source_cache (shared across the whole sync run) plus a re-search
        right before create guarantee no duplicate sources are made even
        when the same new value appears on many rows.
        """
        cell = (cell or '').strip()
        if not cell:
            return False
        key = cell.lower()
        if key in source_cache:
            return source_cache[key]

        Source = self.env['leads.sources']
        source = Source.search([('name', '=ilike', cell)], limit=1)
        if not source and self.auto_create_source:
            # Re-check just before creating (another cron/user may have
            # added it since the cache was built).
            source = Source.search([('name', '=ilike', cell)], limit=1)
            if not source:
                source = Source.create({'name': cell})
                _logger.info(
                    "urbanchat.gsheet.config: auto-created lead source '%s' "
                    "(config %s)", cell, self.name)
        source_cache[key] = source.id if source else False
        return source_cache[key]

    # ── Robust duplicate detection (same rules as leads.logic.create) ───────
    def _find_existing_lead(self, vals):
        """Return (existing_lead_or_False, duplicate_type_or_False).

        Mirrors the duplicate rules that custom_leads_19 enforces inside
        leads.logic.create(): phone matched on the LAST 10 DIGITS (so
        '+91 98765 43210' and '9876543210' collide), email matched
        case-insensitively. Checking here first means duplicates are handled
        per this connector's On Duplicate setting instead of tripping the
        ValidationError inside create()."""
        Leads = self.env['leads.logic'].sudo()
        phone = (vals.get('phone_number') or '').replace(' ', '')
        email = vals.get('email_address') or ''

        phone_match = False
        if phone and self.duplicate_check_field == 'phone_number':
            last_10 = phone[-10:] if len(phone) >= 10 else phone
            phone_match = Leads.search(
                [('phone_number', 'like', '%' + last_10)], limit=1)

        email_match = False
        if email and (self.duplicate_check_field == 'email_address'
                      or phone_match):
            email_match = Leads.search(
                [('email_address', '=ilike', email)], limit=1)

        if phone_match and email_match and phone_match.id == email_match.id:
            return phone_match, 'both'
        if phone_match:
            return phone_match, 'phone'
        if self.duplicate_check_field == 'email_address' and email_match:
            return email_match, 'email'
        return False, False

    # ── Duplicate → Re-Attempt ───────────────────────────────────────────────
    def _create_reattempt_for_duplicate(self, existing, vals, duplicate_type,
                                        owner_name_raw):
        """Create an otomater.lead.reattempt request for a duplicate sheet
        row — same field pattern the manual duplicate-interception in
        custom_leads_19 uses, so it lands in the normal Pending Review
        queue for the Team Lead."""
        reattempt = self.env['otomater.lead.reattempt'].sudo().create({
            'lead_id': existing.id,
            'existing_owner_id': existing.lead_owner.id if existing.lead_owner else False,
            'requested_owner_id': self.env.user.employee_id.id
                if self.env.user.employee_id else False,
            'request_date': fields.Datetime.now(),
            'source_id': vals.get('leads_source', False),
            'remarks': _("Auto-created by Urban Chat sheet sync (%s). "
                         "Sheet owner name: %s") % (
                self.name, owner_name_raw or _('(empty)')),
            'duplicate_type': duplicate_type or 'phone',
            'mobile': vals.get('phone_number', ''),
            'email': vals.get('email_address', ''),
            'review_status': 'pending_review',
            're_attempt_count': (existing.re_attempt_count or 0) + 1,
        })
        return reattempt

    # ── Fetching the sheet ───────────────────────────────────────────────────
    @api.model
    def _normalize_csv_url(self, url):
        """Accept any of the common Google Sheet link shapes and return a
        direct CSV URL:

        * Already a published/export CSV link  -> unchanged
        * Normal sheet link (docs.google.com/spreadsheets/d/<ID>/edit#gid=N)
          -> converted to /export?format=csv&gid=N  (works when the sheet's
          sharing is 'Anyone with the link — Viewer', no publish needed)
        """
        url = (url or '').strip()
        if not url:
            return url
        if 'output=csv' in url or 'format=csv' in url or '/pub?' in url:
            return url
        m = re.search(r'docs\.google\.com/spreadsheets/d/([a-zA-Z0-9\-_]+)', url)
        if m:
            sheet_id = m.group(1)
            gid_match = re.search(r'[#&?]gid=(\d+)', url)
            gid = gid_match.group(1) if gid_match else '0'
            return ('https://docs.google.com/spreadsheets/d/%s/export'
                    '?format=csv&gid=%s' % (sheet_id, gid))
        return url

    @api.model
    def _looks_like_html(self, text):
        head = (text or '').lstrip()[:300].lower()
        return head.startswith('<!doctype') or head.startswith('<html') \
            or '<head' in head[:100]
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
            fetch_url = self._normalize_csv_url(self.csv_url)
            try:
                resp = requests.get(fetch_url, timeout=30, allow_redirects=True)
                resp.raise_for_status()
            except requests.RequestException as e:
                raise UserError(_(
                    "Could not fetch the CSV sheet: %s\n\n"
                    "If this is a 401/403/404: the sheet is not reachable "
                    "without login. Either set Share > 'Anyone with the "
                    "link — Viewer', or use File > Share > Publish to web "
                    "> CSV and paste that link.") % e)
            content = resp.content.decode('utf-8-sig', errors='replace')
            content_type = (resp.headers.get('Content-Type') or '').lower()
            if 'html' in content_type or self._looks_like_html(content):
                raise UserError(_(
                    "Google returned a web page instead of CSV data.\n\n"
                    "This usually means the sheet is NOT publicly "
                    "reachable — Google is showing its login/consent page.\n\n"
                    "Fix one of these ways:\n"
                    "1. In the sheet: Share > General access > 'Anyone with "
                    "the link' > Viewer, then paste the normal sheet URL "
                    "here (it is converted to a CSV export link "
                    "automatically), or\n"
                    "2. File > Share > Publish to web > select the tab > "
                    "CSV > Publish, and paste that published link, or\n"
                    "3. Keep the sheet private and use the Google OAuth "
                    "connection method instead.\n\n"
                    "URL actually fetched: %s") % fetch_url)
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
        result = {'created': 0, 'updated': 0, 'skipped': 0, 'reattempt': 0, 'errors': 0}
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
        source_cache = {}  # lower(name) -> leads.sources id, shared for the run

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
                        source_id = self._resolve_source(cell, source_cache)
                        if source_id:
                            vals['leads_source'] = source_id
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
                                     "(sheet value didn't match, auto-create is "
                                     "off, and no Default Lead Source is set)."),
                    })
                    continue

                vals.setdefault('lead_quality', self.default_quality or 'new')
                vals.setdefault('name', vals.get('phone_number') or 'Urban Chat Lead')

                # ── Duplicate check FIRST (same last-10 phone / ilike email
                #    rules as leads.logic.create), so duplicates never reach
                #    create() and its ValidationError ─────────────────────
                existing, dup_type = self._find_existing_lead(vals)

                if existing:
                    if self.duplicate_action == 'skip':
                        result['skipped'] += 1
                        Log.create({
                            'config_id': self.id, 'row_number': row_num, 'lead_id': existing.id,
                            'status': 'skipped', 'raw_owner_name': owner_name_raw,
                            'message': _("Duplicate by %s — skipped.") % (dup_type or self.duplicate_check_field),
                        })
                        continue

                    if self.duplicate_action == 'reattempt':
                        with self.env.cr.savepoint():
                            reattempt = self._create_reattempt_for_duplicate(
                                existing, vals, dup_type, owner_name_raw)
                        result['reattempt'] += 1
                        Log.create({
                            'config_id': self.id, 'row_number': row_num,
                            'lead_id': existing.id, 'status': 'reattempt',
                            'raw_owner_name': owner_name_raw,
                            'reattempt_id': reattempt.id,
                            'message': _("Duplicate by %s — Re-Attempt %s created "
                                         "(Pending Review).") % (
                                dup_type or self.duplicate_check_field, reattempt.name),
                        })
                        continue

                    # duplicate_action == 'update': only re-assign owner on a
                    # confirmed name match — never burn a round-robin slot on
                    # a lead that already has an owner.
                    matched_employee, match_note = self._match_employee_by_name(owner_name_raw)
                    if matched_employee:
                        vals['lead_owner'] = matched_employee.id
                    with self.env.cr.savepoint():
                        existing.write(vals)
                    result['updated'] += 1
                    Log.create({
                        'config_id': self.id, 'row_number': row_num, 'lead_id': existing.id,
                        'status': 'updated', 'raw_owner_name': owner_name_raw,
                        'matched_employee_id': matched_employee.id if matched_employee else False,
                        'message': match_note or False,
                    })
                    continue

                # ── New lead: owner name match first, round-robin second ──
                matched_employee, match_note = self._match_employee_by_name(owner_name_raw)
                fallback_team = False
                if matched_employee:
                    vals['lead_owner'] = matched_employee.id
                elif self.default_team_ids:
                    fallback_team = self._team_at_cycle_index(team_cycle_idx)
                    team_cycle_idx += 1
                    rr_employee = self._pick_team_round_robin(fallback_team)
                    if rr_employee:
                        vals['lead_owner'] = rr_employee.id

                try:
                    with self.env.cr.savepoint():
                        lead = Leads.create(vals)
                except ValidationError as ve:
                    # A duplicate variant slipped past our check (e.g. the
                    # base module matched on a rule this config isn't
                    # checking). leads.logic.create() has ALREADY created a
                    # re-attempt in its own cursor before raising — record
                    # that instead of an error.
                    result['reattempt'] += 1
                    Log.create({
                        'config_id': self.id, 'row_number': row_num,
                        'status': 'reattempt', 'raw_owner_name': owner_name_raw,
                        'message': _("Duplicate intercepted by lead creation "
                                     "rules — Re-Attempt auto-created. %s"
                                     ) % str(ve)[:300],
                    })
                    continue

                result['created'] += 1
                Log.create({
                    'config_id': self.id, 'row_number': row_num, 'lead_id': lead.id,
                    'status': 'created', 'raw_owner_name': owner_name_raw,
                    'matched_employee_id': matched_employee.id if matched_employee else False,
                    'fallback_team_id': fallback_team.id if fallback_team else False,
                    'message': match_note or False,
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

        summary = _("Created %(c)s, Updated %(u)s, Skipped %(s)s, "
                    "Re-Attempts %(r)s, Errors %(e)s") % {
            'c': result['created'], 'u': result['updated'],
            's': result['skipped'], 'r': result['reattempt'],
            'e': result['errors'],
        }
        self.write({
            'last_sync_date': fields.Datetime.now(),
            'last_sync_summary': summary,
        })
        return result

    # ── Public actions ───────────────────────────────────────────────────────
    def action_test_connection(self):
        """Fetch the sheet and report what was found — WITHOUT creating,
        updating, or importing anything. Verifies the connection works and
        that the configured column headers actually exist in row 1."""
        self.ensure_one()
        rows = self._fetch_rows()  # raises a clear UserError on any failure

        if not rows:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Connection OK — but sheet is empty'),
                    'message': _("The sheet was reached successfully but "
                                 "returned no rows at all."),
                    'type': 'warning',
                    'sticky': True,
                },
            }

        header = [(h or '').strip()[:40] for h in rows[0]]
        header_lower = [h.lower() for h in header]
        data_row_count = sum(
            1 for r in rows[1:] if any((c or '').strip() for c in r))

        found, missing = [], []
        for line in self.column_map_ids:
            key = (line.sheet_header or '').strip().lower()
            (found if key in header_lower else missing).append(
                line.sheet_header)

        parts = [
            _("✓ Sheet reached successfully."),
            _("Header columns (%(n)s): %(h)s") % {
                'n': len(header), 'h': ', '.join(header) or _('(none)')},
            _("Data rows (non-blank): %s") % data_row_count,
        ]
        if found:
            parts.append(_("✓ Mapped headers found: %s") % ', '.join(found))
        if missing:
            parts.append(_("✗ Mapped headers NOT in sheet: %s")
                         % ', '.join(missing))
        if not self.column_map_ids:
            parts.append(_("⚠ No column mapping configured yet."))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Test Connection — %s')
                         % ('Issues Found' if missing or not self.column_map_ids
                            else 'All Good'),
                'message': '\n'.join(parts),
                'type': 'warning' if (missing or not self.column_map_ids)
                        else 'success',
                'sticky': True,
            },
        }

    def action_check_owner_names(self):
        """Dry-run the officer matching for every distinct value in the
        sheet's Lead Owner Name column — nothing is imported. Shows which
        names will match which Admission Officer and why the rest won't."""
        self.ensure_one()
        rows = self._fetch_rows()
        if not rows:
            raise UserError(_("The sheet returned no rows."))

        header = [(h or '').strip().lower() for h in rows[0]]
        owner_col = None
        for line in self.column_map_ids:
            if line.target_field == 'lead_owner_name':
                key = (line.sheet_header or '').strip().lower()
                if key in header:
                    owner_col = header.index(key)
                break
        if owner_col is None:
            raise UserError(_(
                "No column is mapped to 'Lead Owner Name' (or its header "
                "was not found in the sheet). Map it first."))

        distinct = []
        seen = set()
        for row in rows[1:]:
            cell = (row[owner_col] if owner_col < len(row) else '') or ''
            cell = cell.strip()
            key = self._normalize_person_name(cell)
            if cell and key not in seen:
                seen.add(key)
                distinct.append(cell)

        matched_lines, unmatched_lines = [], []
        for name in distinct[:60]:
            emp, note = self._match_employee_by_name(name)
            if emp:
                matched_lines.append("✓ %s  →  %s" % (name, emp.name))
            else:
                unmatched_lines.append("✗ %s%s" % (
                    name, ('  (%s)' % note) if note else ''))

        officer_names = sorted(set(
            m.employee_id.name
            for m in self.env['lead.team.member'].search([])
            if m.employee_id.name))
        tl_names = sorted(set(
            emp.name
            for team in self.env['lead.team'].search([])
            for emp in team.team_lead_ids if emp.name))

        parts = [_("Distinct owner names in sheet: %s") % len(distinct)]
        if matched_lines:
            parts.append(_("MATCHED (%s):") % len(matched_lines))
            parts.extend(matched_lines)
        if unmatched_lines:
            parts.append(_("NOT MATCHED (%s) — these leads will go "
                           "round-robin:") % len(unmatched_lines))
            parts.extend(unmatched_lines)
            parts.append(_("Matching sees Admission Officers who are members "
                           "of a Lead Team AND Team Leads. Officers: %s. "
                           "Team Leads: %s") % (
                ', '.join(officer_names) or _('(none)'),
                ', '.join(tl_names) or _('(none)')))
            parts.append(_("To fix a name: add that person's Employee to a "
                           "Lead Team, rename so the names correspond "
                           "(punctuation and case are ignored), or add an "
                           "Owner Name Alias on this connector — e.g. "
                           "'Sajina Faizal' → SAJINA. A."))

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Owner Name Check — %s matched, %s not') % (
                    len(matched_lines), len(unmatched_lines)),
                'message': '\n'.join(parts),
                'type': 'warning' if unmatched_lines else 'success',
                'sticky': True,
            },
        }

    def action_sync_now(self):
        self.ensure_one()
        result = self._sync()
        message = _(
            "Created: %(c)s   Updated: %(u)s   Skipped: %(s)s   "
            "Re-Attempts: %(r)s   Errors: %(e)s"
        ) % {
            'c': result['created'], 'u': result['updated'],
            's': result['skipped'], 'r': result['reattempt'],
            'e': result['errors'],
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

class UrbanchatOwnerAlias(models.Model):
    _name = 'urbanchat.owner.alias'
    _description = 'Sheet Owner Name Alias -> Admission Officer'
    _rec_name = 'alias'

    config_id = fields.Many2one(
        'urbanchat.gsheet.config', string='Connector', required=True,
        ondelete='cascade')
    alias = fields.Char(
        string='Sheet Name Variant', required=True,
        help="A name exactly as it appears in the sheet's Lead Owner Name "
             "column (punctuation and case are ignored when matching), "
             "e.g. 'FOUSIYA LATHEEF' or 'Sajina Faizal'.")
    employee_id = fields.Many2one(
        'hr.employee', string='Admission Officer', required=True,
        help="The officer this variant refers to. Should be a member of a "
             "Lead Team so the assignment behaves like a normal match.")

    _sql_constraints = [
        ('alias_config_uniq', 'unique(config_id, alias)',
         'This alias already exists on this connector.'),
    ]

