#       Licensed to the Apache Software Foundation (ASF) under one
#       or more contributor license agreements.  See the NOTICE file
#       distributed with this work for additional information
#       regarding copyright ownership.  The ASF licenses this file
#       to you under the Apache License, Version 2.0 (the
#       "License"); you may not use this file except in compliance
#       with the License.  You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#       Unless required by applicable law or agreed to in writing,
#       software distributed under the License is distributed on an
#       "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
#       KIND, either express or implied.  See the License for the
#       specific language governing permissions and limitations
#       under the License.

#-*- python -*-
import logging
import json
import re
from datetime import datetime
from cStringIO import StringIO

# Non-stdlib imports
from pylons import tmpl_context as c
from allura.lib import helpers as h

from ming.orm.ormsession import ThreadLocalORMSession

# Pyforge-specific imports
from allura import model as M

# Local imports
from forgetracker import model as TM

try:
    from forgeimporters.base import ProjectExtractor
    urlopen = ProjectExtractor.urlopen
except ImportError:
    urlopen = h.urlopen

log = logging.getLogger(__name__)

class ImportException(Exception):
    pass

class ResettableStream(object):
    '''Class supporting seeks within a header of otherwise
    unseekable stream.'''

    # Seeks are supported with header of this size
    HEADER_BUF_SIZE = 8192

    def __init__(self, fp, header_size=-1):
        self.fp = fp
        self.buf = None
        self.buf_size = header_size if header_size >= 0 else self.HEADER_BUF_SIZE
        self.buf_pos = 0
        self.stream_pos = 0

    def _read_header(self):
        if self.buf is None:
            data = self.fp.read(self.buf_size)
            self.buf = StringIO(data)
            self.buf_len = len(data)
            self.stream_pos = self.buf_len

    def read(self, size=-1):
        self._read_header()
        data = ''
        if self.buf_pos < self.stream_pos:
            data = self.buf.read(size)
            self.buf_pos += len(data)
            if len(data) == size:
                return data
            size -= len(data)

        data += self.fp.read(size)
        self.stream_pos += len(data)
        return data

    def seek(self, pos):
        self._read_header()
        if self.stream_pos > self.buf_len:
            assert False, 'Started reading stream body, cannot reset pos'
        self.buf.seek(pos)
        self.buf_pos = pos

    def tell(self):
        if self.buf_pos < self.stream_pos:
            return self.buf_pos
        else:
            return self.stream_pos

class ImportSupport(object):

    ATTACHMENT_SIZE_LIMIT = 1024*1024

    def __init__(self):
        # Map JSON interchange format fields to Ticket fields
        # key is JSON's field name, value is:
        #   None - drop
        #   True - map as is
        #   (new_field_name, value_convertor(val)) - use new field name and convert JSON's value
        #   handler(ticket, field, val) - arbitrary transform, expected to modify ticket in-place
        self.FIELD_MAP = {
            'assigned_to': ('assigned_to_id', self.get_user_id),
            'class': None,
            'date': ('created_date', self.parse_date),
            'date_updated': ('mod_date', self.parse_date),
            'description': True,
            'id': None,
            'keywords': ('labels', lambda s: s.split()), # default way of handling, see below for overrides
            'status': True,
            'submitter': ('reported_by_id', self.get_user_id),
            'summary': True,
        }
        self.user_map = {}
        self.warnings = []
        self.errors = []
        self.options = {}


    def init_options(self, options_json):
        self.options = json.loads(options_json)
        opt_keywords = self.option('keywords_as', 'split_labels')
        if opt_keywords == 'single_label':
            self.FIELD_MAP['keywords'] = ('labels', lambda s: [s])
        elif opt_keywords == 'custom':
            del self.FIELD_MAP['keywords']

    def option(self, name, default=None):
        return self.options.get(name, False)


    #
    # Field/value convertors
    #
    @staticmethod
    def parse_date(date_string):
        return datetime.strptime(date_string, '%Y-%m-%dT%H:%M:%SZ')

    def get_user_id(self, username):
        def _get_user_id(username):
            u = M.User.by_username(username)
            return u._id if u else None

        if self.options.get('usernames_match'):
            return _get_user_id(username)

        mapped_username = self.options['user_map'].get(username)
        if mapped_username:
            return _get_user_id(mapped_username)

        return None

    def check_custom_field(self, field, value, ticket_status):
        field = c.app.globals.get_custom_field(field)
        if (field['type'] == 'select') and value:
            field_options = h.split_select_field_options(h.really_unicode(field['options']))
            if value not in field_options:
                field['options'] = ' '.join([field['options'], value])
        elif (field['type'] == 'milestone') and value:
            milestones = field['milestones']
            for milestone in milestones:
                if milestone['name'] == value:
                    if ticket_status in c.app.globals.open_status_names:
                        milestone['complete'] = False
                    break
            else:
                milestone = {'due_date': '',
                             'complete': not ticket_status in c.app.globals.open_status_names,
                             'description': '',
                             'name': value,
                             'old_name': value}
                field['milestones'].append(milestone)
        ThreadLocalORMSession.flush_all()

    def custom(self, ticket, field, value, ticket_status):
        field = '_' + field
        if not c.app.has_custom_field(field):
            log.warning('Custom field %s is not defined, defining as string', field)
            c.app.globals.custom_fields.append(dict(name=field, label=field[1:].capitalize(), type='string'))
            ThreadLocalORMSession.flush_all()
        if 'custom_fields' not in ticket:
            ticket['custom_fields'] = {}
        self.check_custom_field(field, value, ticket_status)
        ticket['custom_fields'][field] = value

    def make_artifact(self, ticket_dict):
        remapped = {}
        for f, v in ticket_dict.iteritems():
            transform = self.FIELD_MAP.get(f, ())
            if transform is None:
                continue
            elif transform is True:
                remapped[f] = v
            elif callable(transform):
                transform(remapped, f, v)
            elif transform is ():
                self.custom(remapped, f, v, ticket_dict.get('status'))
            else:
                new_f, conv = transform
                remapped[new_f] = conv(v)

        description = h.really_unicode(self.link_processing(remapped['description']))
        creator = owner = ''
        if ticket_dict['submitter'] and not remapped['reported_by_id']:
            creator = u'*Originally created by:* {0}\n'.format(
                    h.really_unicode(ticket_dict['submitter']))
        if ticket_dict['assigned_to'] and not remapped['assigned_to_id']:
            owner = u'*Originally owned by:* {0}\n'.format(
                    h.really_unicode(ticket_dict['assigned_to']))
        remapped['description'] = u'{0}{1}{2}{3}'.format(creator, owner,
                '\n' if creator or owner else '', description)

        ticket_num = ticket_dict['id']
        existing_ticket = TM.Ticket.query.get(app_config_id=c.app.config._id,
                                          ticket_num=ticket_num)
        if existing_ticket:
            ticket_num = c.app.globals.next_ticket_num()
            self.warnings.append('Ticket #%s: Ticket with this id already exists, using next available id: %s' % (ticket_dict['id'], ticket_num))
        else:
            if c.app.globals.last_ticket_num < ticket_num:
                c.app.globals.last_ticket_num = ticket_num
                ThreadLocalORMSession.flush_all()

        ticket = TM.Ticket(
            app_config_id=c.app.config._id,
            custom_fields=dict(),
            ticket_num=ticket_num,
            import_id=c.api_token.api_key)
        ticket.update(remapped)
        return ticket

    def ticket_link(self, m):
        return '(%s)' % m.groups()[0]

    def ticket_bracket_link(self, m):
        text = m.groups()[0]
        return '[\[%s\]](%s:#%s)' % (text, c.app.config.options.mount_point, text)

    def get_slug_by_id(self, ticket, comment):
        comment = int(comment)
        ticket = TM.Ticket.query.get(app_config_id=c.app.config._id,
                                     ticket_num=int(ticket))
        if not ticket:
            return ''
        comments = ticket.discussion_thread.post_class().query.find(dict(
            discussion_id=ticket.discussion_thread.discussion_id,
            thread_id=ticket.discussion_thread._id,
            status={'$in': ['ok', 'pending']})).sort('timestamp')

        if comment <= comments.count():
            return comments.all()[comment-1].slug

    def comment_link(self, m):
        text, ticket, comment = m.groups()
        ticket = ticket.replace('\n', '')
        text = text.replace('\n', ' ')
        slug = self.get_slug_by_id(ticket, comment)
        if slug:
            return '[%s](%s/#%s)' % (text, ticket, slug)
        else:
            return text

    def brackets_escaping(self, m):
        return '[\[%s\]]' % m.groups()[0]

    def link_processing(self, text):
        short_link_ticket_pattern = re.compile('(?<!\[)#(\d+)(?!\])')
        comment_pattern = re.compile('\[(\S*\s*\S*)\]\(\S*/(\d+\n*\d*)#comment:(\d+)\)')
        ticket_pattern = re.compile('(?<=\])\(\S*ticket/(\d+)\)')
        brackets_pattern = re.compile('\[\[(.*)\]\]')

        text = short_link_ticket_pattern.sub(self.ticket_bracket_link, text)
        text = comment_pattern.sub(self.comment_link, text)
        text = ticket_pattern.sub(self.ticket_link, text)
        text = brackets_pattern.sub(self.brackets_escaping, text)
        return text

    def make_comment(self, thread, comment_dict):
        ts = self.parse_date(comment_dict['date'])
        author_id = self.get_user_id(comment_dict['submitter'])
        text = h.really_unicode(self.link_processing(comment_dict['comment']))
        if not author_id and comment_dict['submitter']:
            text = u'Originally posted by: {0}\n\n{1}'.format(
                    h.really_unicode(comment_dict['submitter']), text)
        comment = thread.post(text=text, timestamp=ts)
        comment.author_id = author_id
        comment.import_id = c.api_token.api_key

    def make_attachment(self, org_ticket_id, ticket_id, att_dict):
        if att_dict['size'] > self.ATTACHMENT_SIZE_LIMIT:
            self.errors.append('Ticket #%s: Attachment %s (@ %s) is too large, skipping' %
                               (org_ticket_id, att_dict['filename'], att_dict['url']))
            return
        f = urlopen(att_dict['url'])
        TM.TicketAttachment.save_attachment(att_dict['filename'], ResettableStream(f),
                                            artifact_id=ticket_id)


    #
    # User handling
    #
    def collect_users(self, artifacts):
        users = set()
        for a in artifacts:
            users.add(a['submitter'])
            users.add(a['assigned_to'])
            for c in a['comments']:
                users.add(c['submitter'])
        return users

    def find_unknown_users(self, users):
        unknown = set()
        for u in users:
            if u and not u in self.options['user_map'] and not M.User.by_username(u):
                unknown.add(u)
        return unknown

    def make_user_placeholders(self, usernames):
        for username in usernames:
            allura_username = username
            if self.option('create_users') != '_unprefixed':
                allura_username = c.project.shortname + '-' + username
            M.User.register(dict(username=allura_username,
                                 display_name=username), False)
            self.options['user_map'][username] = allura_username
        ThreadLocalORMSession.flush_all()
        log.info('Created %d user placeholders', len(usernames))

    def validate_user_mapping(self):
        if 'user_map' not in self.options:
            self.options['user_map'] = {}
        for foreign_user, allura_user in self.options['user_map'].iteritems():
            u = M.User.by_username(allura_user)
            if not u:
                raise ImportException('User mapping %s:%s - target user does not exist' % (foreign_user, allura_user))


    #
    # Main methods
    #
    def validate_import(self, doc, options, **post_data):
        log.info('validate_migration called: %s', doc)
        self.init_options(options)
        log.info('options: %s', self.options)
        self.validate_user_mapping()

        project_doc = json.loads(doc)
        tracker_names = project_doc['trackers'].keys()
        if len(tracker_names) > 1:
            self.errors.append('Only single tracker import is supported')
            return self.errors, self.warnings
        artifacts = project_doc['trackers'][tracker_names[0]]['artifacts']
        users = self.collect_users(artifacts)
        unknown_users = self.find_unknown_users(users)
        unknown_users = sorted(list(unknown_users))
        if unknown_users:
            self.warnings.append('''Document references unknown users. You should provide
option user_map to avoid losing username information. Unknown users: %s''' % unknown_users)

        return {'status': True, 'errors': self.errors, 'warnings': self.warnings}

    def perform_import(self, doc, options, **post_data):
        log.info('import called: %s', options)
        self.init_options(options)
        self.validate_user_mapping()

        project_doc = json.loads(doc)
        tracker_names = project_doc['trackers'].keys()
        if len(tracker_names) > 1:
            self.errors.append('Only single tracker import is supported')
            return self.errors, self.warnings

        log.info('Import id: %s', c.api_token.api_key)

        artifacts = project_doc['trackers'][tracker_names[0]]['artifacts']

        if self.option('create_users'):
            users = self.collect_users(artifacts)
            unknown_users = self.find_unknown_users(users)
            self.make_user_placeholders(unknown_users)

        M.session.artifact_orm_session._get().skip_mod_date = True
        for a in artifacts:
            comments = a.pop('comments', [])
            attachments = a.pop('attachments', [])
            t = self.make_artifact(a)
            for c_entry in comments:
                self.make_comment(t.discussion_thread, c_entry)
            for a_entry in attachments:
                try:
                    self.make_attachment(a['id'], t._id, a_entry)
                except Exception, e:
                    self.warnings.append('Could not import attachment, skipped: %s' % e)
            log.info('Imported ticket: %d', t.ticket_num)
        c.app.globals.invalidate_bin_counts()

        return {'status': True, 'errors': self.errors, 'warnings': self.warnings}
