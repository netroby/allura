import json
import hmac
import hashlib
import datetime as dt

from mock import Mock, patch
from nose.tools import (
    assert_raises,
    assert_equal,
    assert_not_in,
    assert_in,
)
from formencode import Invalid
from ming.odm import session
from pylons import tmpl_context as c
from tg import config

from allura import model as M
from allura.lib import helpers as h
from allura.lib.utils import DateJSONEncoder
from allura.webhooks import (
    MingOneOf,
    WebhookValidator,
    WebhookController,
    send_webhook,
    RepoPushWebhookSender,
)
from allura.tests import decorators as td
from alluratest.controller import setup_basic_test, TestController


# important to be distinct from 'test' and 'test2' which ForgeGit and
# ForgeImporter use, so that the tests can run in parallel and not clobber each
# other
test_project_with_repo = 'adobe-1'
with_git = td.with_tool(test_project_with_repo, 'git', 'src', 'Git')
with_git2 = td.with_tool(test_project_with_repo, 'git', 'src2', 'Git2')


class TestWebhookBase(object):

    def setUp(self):
        setup_basic_test()
        self.setup_with_tools()
        self.project = M.Project.query.get(shortname=test_project_with_repo)
        self.git = self.project.app_instance('src')
        self.wh = M.Webhook(
            type='repo-push',
            app_config_id=self.git.config._id,
            hook_url='http://httpbin.org/post',
            secret='secret')
        session(self.wh).flush(self.wh)

    @with_git
    def setup_with_tools(self):
        pass


class TestValidators(TestWebhookBase):

    def test_ming_one_of(self):
        ids = [ac._id for ac in M.AppConfig.query.find().all()[:2]]
        v = MingOneOf(cls=M.AppConfig, ids=ids, not_empty=True)
        with assert_raises(Invalid) as cm:
            v.to_python(None)
        assert_equal(cm.exception.msg, u'Please enter a value')
        with assert_raises(Invalid) as cm:
            v.to_python('invalid id')
        assert_equal(cm.exception.msg,
            u'Object must be one of: %s, not invalid id' % ids)
        assert_equal(v.to_python(ids[0]), M.AppConfig.query.get(_id=ids[0]))
        assert_equal(v.to_python(ids[1]), M.AppConfig.query.get(_id=ids[1]))
        assert_equal(v.to_python(unicode(ids[0])),
                     M.AppConfig.query.get(_id=ids[0]))
        assert_equal(v.to_python(unicode(ids[1])),
                     M.AppConfig.query.get(_id=ids[1]))

    def test_webhook_validator(self):
        sender = Mock(type='repo-push')
        ids = [ac._id for ac in M.AppConfig.query.find().all()[:3]]
        ids, invalid_id = ids[:2], ids[2]
        v = WebhookValidator(sender=sender, ac_ids=ids, not_empty=True)
        with assert_raises(Invalid) as cm:
            v.to_python(None)
        assert_equal(cm.exception.msg, u'Please enter a value')
        with assert_raises(Invalid) as cm:
            v.to_python('invalid id')
        assert_equal(cm.exception.msg, u'Invalid webhook')

        wh = M.Webhook(type='invalid type',
                       app_config_id=invalid_id,
                       hook_url='http://httpbin.org/post',
                       secret='secret')
        session(wh).flush(wh)
        with assert_raises(Invalid) as cm:
            v.to_python(wh._id)
        assert_equal(cm.exception.msg, u'Invalid webhook')

        wh.type = 'repo-push'
        session(wh).flush(wh)
        with assert_raises(Invalid) as cm:
            v.to_python(wh._id)
        assert_equal(cm.exception.msg, u'Invalid webhook')

        wh.app_config_id = ids[0]
        session(wh).flush(wh)
        assert_equal(v.to_python(wh._id), wh)
        assert_equal(v.to_python(unicode(wh._id)), wh)


class TestWebhookController(TestController):

    def setUp(self):
        super(TestWebhookController, self).setUp()
        self.setup_with_tools()
        self.patches = self.monkey_patch()
        for p in self.patches:
            p.start()
        self.project = M.Project.query.get(shortname=test_project_with_repo)
        self.git = self.project.app_instance('src')
        self.git2 = self.project.app_instance('src2')
        self.url = str(self.project.url() + 'admin/webhooks')

    def tearDown(self):
        super(TestWebhookController, self).tearDown()
        for p in self.patches:
            p.stop()

    @with_git
    @with_git2
    def setup_with_tools(self):
        pass

    def monkey_patch(self):
        gen_secret = patch.object(
            WebhookController,
            'gen_secret',
            return_value='super-secret',
            autospec=True)
        return [gen_secret]

    def create_webhook(self, data):
        r = self.app.post(self.url + '/repo-push/create', data)
        wf = json.loads(self.webflash(r))
        assert_equal(wf['status'], 'ok')
        assert_equal(wf['message'], 'Created successfully')
        return r

    def find_error(self, r, field, msg, form_type='create'):
        form = r.html.find('form', attrs={'action': form_type})
        if field == '_the_form':
            error = form.findPrevious('div', attrs={'class': 'error'})
        else:
            widget = 'select' if field == 'app' else 'input'
            error = form.find(widget, attrs={'name': field})
            error = error.findNext('div', attrs={'class': 'error'})
        if error:
            assert_in(h.escape(msg), error.getText())
        else:
            assert False, 'Validation error not found'

    def test_access(self):
        self.app.get(self.url + '/repo-push/')
        self.app.get(self.url + '/repo-push/',
                     extra_environ={'username': 'test-user'},
                     status=403)
        r = self.app.get(self.url + '/repo-push/',
                         extra_environ={'username': '*anonymous'},
                         status=302)
        assert_equal(r.location,
            'http://localhost/auth/'
            '?return_to=%2Fadobe%2Fadobe-1%2Fadmin%2Fwebhooks%2Frepo-push%2F')

    def test_invalid_hook_type(self):
        self.app.get(self.url + '/invalid-hook-type/', status=404)

    def test_create(self):
        assert_equal(M.Webhook.query.find().count(), 0)
        r = self.app.get(self.url)
        assert_in('<h1>repo-push</h1>', r)
        assert_not_in('http://httpbin.org/post', r)
        data = {'url': u'http://httpbin.org/post',
                'app': unicode(self.git.config._id),
                'secret': ''}
        msg = 'add webhook repo-push {} {}'.format(
            data['url'], self.git.config.url())
        with td.audits(msg):
            r = self.create_webhook(data).follow().follow(status=200)
        assert_in('http://httpbin.org/post', r)

        hooks = M.Webhook.query.find().all()
        assert_equal(len(hooks), 1)
        assert_equal(hooks[0].type, 'repo-push')
        assert_equal(hooks[0].hook_url, 'http://httpbin.org/post')
        assert_equal(hooks[0].app_config_id, self.git.config._id)
        assert_equal(hooks[0].secret, 'super-secret')

        # Try to create duplicate
        with td.out_audits(msg):
            r = self.app.post(self.url + '/repo-push/create', data)
        self.find_error(r, '_the_form',
            '"repo-push" webhook already exists for Git http://httpbin.org/post')
        assert_equal(M.Webhook.query.find().count(), 1)

    def test_create_validation(self):
        assert_equal(M.Webhook.query.find().count(), 0)
        r = self.app.post(
            self.url + '/repo-push/create', {}, status=404)

        data = {'url': '', 'app': '', 'secret': ''}
        r = self.app.post(self.url + '/repo-push/create', data)
        self.find_error(r, 'url', 'Please enter a value')
        self.find_error(r, 'app', 'Please enter a value')

        data = {'url': 'qwer', 'app': '123', 'secret': 'qwe'}
        r = self.app.post(self.url + '/repo-push/create', data)
        self.find_error(r, 'url',
            'You must provide a full domain name (like qwer.com)')
        self.find_error(r, 'app', 'Object must be one of: ')
        self.find_error(r, 'app', '%s' % self.git.config._id)
        self.find_error(r, 'app', '%s' % self.git2.config._id)

    def test_edit(self):
        data1 = {'url': u'http://httpbin.org/post',
                 'app': unicode(self.git.config._id),
                 'secret': u'secret'}
        data2 = {'url': u'http://example.com/hook',
                 'app': unicode(self.git2.config._id),
                 'secret': u'secret2'}
        self.create_webhook(data1).follow().follow(status=200)
        self.create_webhook(data2).follow().follow(status=200)
        assert_equal(M.Webhook.query.find().count(), 2)
        wh1 = M.Webhook.query.get(hook_url=data1['url'])
        r = self.app.get(self.url + '/repo-push/%s' % wh1._id)
        form = r.forms[0]
        assert_equal(form['url'].value, data1['url'])
        assert_equal(form['app'].value, data1['app'])
        assert_equal(form['secret'].value, data1['secret'])
        assert_equal(form['webhook'].value, unicode(wh1._id))
        form['url'] = 'http://host.org/hook'
        form['app'] = unicode(self.git2.config._id)
        form['secret'] = 'new secret'
        msg = 'edit webhook repo-push\n{} => {}\n{} => {}\n{}'.format(
            data1['url'], form['url'].value,
            self.git.config.url(), self.git2.config.url(),
            'secret changed')
        with td.audits(msg):
            r = form.submit()
        wf = json.loads(self.webflash(r))
        assert_equal(wf['status'], 'ok')
        assert_equal(wf['message'], 'Edited successfully')
        assert_equal(M.Webhook.query.find().count(), 2)
        wh1 = M.Webhook.query.get(_id=wh1._id)
        assert_equal(wh1.hook_url, 'http://host.org/hook')
        assert_equal(wh1.app_config_id, self.git2.config._id)
        assert_equal(wh1.secret, 'new secret')
        assert_equal(wh1.type, 'repo-push')

        # Duplicates
        r = self.app.get(self.url + '/repo-push/%s' % wh1._id)
        form = r.forms[0]
        form['url'] = data2['url']
        form['app'] = data2['app']
        r = form.submit()
        self.find_error(r, '_the_form',
            u'"repo-push" webhook already exists for Git2 http://example.com/hook',
            form_type='edit')

    def test_edit_validation(self):
        invalid = M.Webhook(
            type='invalid type',
            app_config_id=None,
            hook_url='http://httpbin.org/post',
            secret='secret')
        session(invalid).flush(invalid)
        self.app.get(self.url + '/repo-push/%s' % invalid._id, status=404)

        data = {'url': u'http://httpbin.org/post',
                'app': unicode(self.git.config._id),
                'secret': u'secret'}
        self.create_webhook(data).follow().follow(status=200)
        wh = M.Webhook.query.get(hook_url=data['url'], type='repo-push')

        # invalid id in hidden field, just in case
        r = self.app.get(self.url + '/repo-push/%s' % wh._id)
        data = {k: v[0].value for (k, v) in r.forms[0].fields.items()}
        data['webhook'] = unicode(invalid._id)
        self.app.post(self.url + '/repo-push/edit', data, status=404)

        # empty values
        data = {'url': '', 'app': '', 'secret': '', 'webhook': str(wh._id)}
        r = self.app.post(self.url + '/repo-push/edit', data)
        self.find_error(r, 'url', 'Please enter a value', 'edit')
        self.find_error(r, 'app', 'Please enter a value', 'edit')

        data = {'url': 'qwe', 'app': '123', 'secret': 'qwe',
                'webhook': str(wh._id)}
        r = self.app.post(self.url + '/repo-push/edit', data)
        self.find_error(r, 'url',
            'You must provide a full domain name (like qwe.com)', 'edit')
        self.find_error(r, 'app', 'Object must be one of:', 'edit')
        self.find_error(r, 'app', '%s' % self.git.config._id, 'edit')
        self.find_error(r, 'app', '%s' % self.git2.config._id, 'edit')

    def test_delete(self):
        data = {'url': u'http://httpbin.org/post',
                'app': unicode(self.git.config._id),
                'secret': u'secret'}
        self.create_webhook(data).follow().follow(status=200)
        assert_equal(M.Webhook.query.find().count(), 1)
        wh = M.Webhook.query.get(hook_url=data['url'])
        data = {'webhook': unicode(wh._id)}
        msg = 'delete webhook repo-push {} {}'.format(
            wh.hook_url, self.git.config.url())
        with td.audits(msg):
            r = self.app.post(self.url + '/repo-push/delete', data)
        assert_equal(r.json, {'status': 'ok'})
        assert_equal(M.Webhook.query.find().count(), 0)

    def test_delete_validation(self):
        invalid = M.Webhook(
            type='invalid type',
            app_config_id=None,
            hook_url='http://httpbin.org/post',
            secret='secret')
        session(invalid).flush(invalid)
        assert_equal(M.Webhook.query.find().count(), 1)

        data = {'webhook': ''}
        self.app.post(self.url + '/repo-push/delete', data, status=404)

        data = {'webhook': unicode(invalid._id)}
        self.app.post(self.url + '/repo-push/delete', data, status=404)
        assert_equal(M.Webhook.query.find().count(), 1)

    def test_list_webhooks(self):
        data1 = {'url': u'http://httpbin.org/post',
                 'app': unicode(self.git.config._id),
                 'secret': 'secret'}
        data2 = {'url': u'http://another-host.org/',
                 'app': unicode(self.git2.config._id),
                 'secret': 'secret2'}
        self.create_webhook(data1).follow().follow(status=200)
        self.create_webhook(data2).follow().follow(status=200)
        wh1 = M.Webhook.query.get(hook_url=data1['url'])
        wh2 = M.Webhook.query.get(hook_url=data2['url'])

        r = self.app.get(self.url)
        assert_in('<h1>repo-push</h1>', r)
        rows = r.html.find('table').findAll('tr')
        assert_equal(len(rows), 2)
        rows = sorted([self._format_row(row) for row in rows])
        expected_rows = sorted([
            [{'href': self.url + '/repo-push/' + str(wh1._id),
              'text': wh1.hook_url},
             {'href': self.git.url,
              'text': self.git.config.options.mount_label},
             {'text': wh1.secret},
             {'href': self.url + '/repo-push/delete',
              'data-id': str(wh1._id)}],
            [{'href': self.url + '/repo-push/' + str(wh2._id),
              'text': wh2.hook_url},
             {'href': self.git2.url,
              'text': self.git2.config.options.mount_label},
             {'text': wh2.secret},
             {'href': self.url + '/repo-push/delete',
              'data-id': str(wh2._id)}],
        ])
        assert_equal(rows, expected_rows)

    def _format_row(self, row):
        def link(td):
            a = td.find('a')
            return {'href': a.get('href'), 'text': a.getText()}
        def text(td):
            return {'text': td.getText()}
        def delete_btn(td):
            a = td.find('a')
            return {'href': a.get('href'), 'data-id': a.get('data-id')}
        tds = row.findAll('td')
        return [link(tds[0]), link(tds[1]), text(tds[2]), delete_btn(tds[3])]


class TestTasks(TestWebhookBase):

    @patch('allura.webhooks.requests', autospec=True)
    @patch('allura.webhooks.log', autospec=True)
    def test_send_webhook(self, log, requests):
        requests.post.return_value = Mock(status_code=200)
        payload = {'some': ['data']}
        json_payload = json.dumps(payload, cls=DateJSONEncoder)
        send_webhook(self.wh._id, payload)
        signature = hmac.new(
            self.wh.secret.encode('utf-8'),
            json_payload.encode('utf-8'),
            hashlib.sha1)
        signature = 'sha1=' + signature.hexdigest()
        headers = {'content-type': 'application/json',
                   'User-Agent': 'Allura Webhook (https://allura.apache.org/)',
                   'X-Allura-Signature': signature}
        requests.post.assert_called_once_with(
            self.wh.hook_url,
            data=json_payload,
            headers=headers,
            timeout=30)
        log.info.assert_called_once_with(
            'Webhook successfully sent: %s %s %s',
            self.wh.type, self.wh.hook_url, self.wh.app_config.url())

    @patch('allura.webhooks.requests', autospec=True)
    @patch('allura.webhooks.log', autospec=True)
    def test_send_webhook_error(self, log, requests):
        requests.post.return_value = Mock(status_code=500)
        send_webhook(self.wh._id, {})
        assert_equal(requests.post.call_count, 1)
        assert_equal(log.info.call_count, 0)
        log.error.assert_called_once_with(
            'Webhook send error: %s %s %s %s %s',
            self.wh.type, self.wh.hook_url,
            self.wh.app_config.url(),
            requests.post.return_value.status_code,
            requests.post.return_value.reason)

class TestRepoPushWebhookSender(TestWebhookBase):

    @patch('allura.webhooks.send_webhook', autospec=True)
    def test_send(self, send_webhook):
        sender = RepoPushWebhookSender()
        sender.get_payload = Mock()
        with h.push_config(c, app=self.git):
            sender.send(arg1=1, arg2=2)
        send_webhook.post.assert_called_once_with(
            self.wh._id,
            sender.get_payload.return_value)

    @patch('allura.webhooks.log', autospec=True)
    @patch('allura.webhooks.send_webhook', autospec=True)
    def test_send_limit_reached(self, send_webhook, log):
        sender = RepoPushWebhookSender()
        sender.get_payload = Mock()
        self.wh.enforce_limit = Mock(return_value=False)
        with h.push_config(c, app=self.git):
            sender.send(arg1=1, arg2=2)
        assert_equal(send_webhook.post.call_count, 0)
        log.warn.assert_called_once_with(
            'Webhook fires too often: %s. Skipping', self.wh)

    @patch('allura.webhooks.send_webhook', autospec=True)
    def test_send_no_configured_webhooks(self, send_webhook):
        self.wh.delete()
        session(self.wh).flush(self.wh)
        sender = RepoPushWebhookSender()
        with h.push_config(c, app=self.git):
            sender.send(arg1=1, arg2=2)
        assert_equal(send_webhook.post.call_count, 0)

    def test_get_payload(self):
        sender = RepoPushWebhookSender()
        _ci = list(range(1, 4))
        _se = [Mock(info=str(x)) for x in _ci]
        with patch.object(self.git.repo, 'commit', autospec=True, side_effect=_se):
            with h.push_config(c, app=self.git):
                result = sender.get_payload(commit_ids=_ci)
        expected_result = {
            'url': 'http://localhost/adobe/adobe-1/src/',
            'count': 3,
            'revisions': ['1', '2', '3'],
        }
        assert_equal(result, expected_result)


class TestModels(TestWebhookBase):

    def test_webhook_find(self):
        p = M.Project.query.get(shortname='test')
        assert_equal(M.Webhook.find('smth', p), [])
        assert_equal(M.Webhook.find('repo-push', p), [])
        assert_equal(M.Webhook.find('smth', self.project), [])
        assert_equal(M.Webhook.find('repo-push', self.project), [self.wh])

    def test_webhook_url(self):
        assert_equal(self.wh.url(),
            '/adobe/adobe-1/admin/webhooks/repo-push/{}'.format(self.wh._id))

    def test_webhook_enforce_limit(self):
        self.wh.last_sent = None
        assert_equal(self.wh.enforce_limit(), True)
        # default value
        self.wh.last_sent = dt.datetime.utcnow() - dt.timedelta(seconds=31)
        assert_equal(self.wh.enforce_limit(), True)
        self.wh.last_sent = dt.datetime.utcnow() - dt.timedelta(seconds=15)
        assert_equal(self.wh.enforce_limit(), False)
        # value from config
        with h.push_config(config, **{'webhook.repo_push.limit': 100}):
            self.wh.last_sent = dt.datetime.utcnow() - dt.timedelta(seconds=101)
            assert_equal(self.wh.enforce_limit(), True)
            self.wh.last_sent = dt.datetime.utcnow() - dt.timedelta(seconds=35)
            assert_equal(self.wh.enforce_limit(), False)

    @patch('allura.model.webhook.dt', autospec=True)
    def test_update_limit(self, dt_mock):
        _now = dt.datetime(2015, 02, 02, 13, 39)
        dt_mock.datetime.utcnow.return_value = _now
        assert_equal(self.wh.last_sent, None)
        self.wh.update_limit()
        session(self.wh).expunge(self.wh)
        assert_equal(M.Webhook.query.get(_id=self.wh._id).last_sent, _now)