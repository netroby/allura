import pkg_resources
import unittest

from pylons import app_globals as g
from pylons import tmpl_context as c

from alluratest.controller import TestController, setup_basic_test, setup_global_objects
from allura.tests import decorators as td
from allura.lib import helpers as h
from allura.model import User
from allura import model as M

from forgegit.tests import with_git
from forgewiki import model as WM
from forgetracker import model as TM

class TestStats(TestController):

    @td.with_user_project('test-user')
    def test_init_values(self):
        user = User.register(dict(username='test-new-user',
            display_name='Test Stats'),
            make_project=False)

        artifacts = user.stats.getArtifacts()
        tickets = user.stats.getTickets()
        commits = user.stats.getCommits()
        assert user.stats.tot_logins_count == 0
        assert artifacts['created'] == 0
        assert artifacts['modified'] == 0
        assert tickets['assigned'] == 0
        assert tickets['solved'] == 0
        assert tickets['revoked'] == 0
        assert tickets['averagesolvingtime'] is None
        assert commits['number'] == 0
        assert commits['lines'] == 0

        lmartifacts = user.stats.getLastMonthArtifacts()
        lmtickets = user.stats.getLastMonthTickets()
        lmcommits = user.stats.getLastMonthCommits()
        assert user.stats.getLastMonthLogins() == 0
        assert lmartifacts['created'] == 0
        assert lmartifacts['modified'] == 0
        assert lmtickets['assigned'] == 0
        assert lmtickets['solved'] == 0
        assert lmtickets['revoked'] == 0
        assert lmtickets['averagesolvingtime'] is None
        assert lmcommits['number'] == 0
        assert lmcommits['lines'] == 0

    def test_login(self):
        user = User.by_username('test-user')
        init_logins = c.user.stats.tot_logins_count
        r = self.app.post('/auth/do_login', params=dict(
                username=user.username, password='foo'))

        assert user.stats.tot_logins_count == 1 + init_logins
        assert user.stats.getLastMonthLogins() == 1 + init_logins

    @td.with_user_project('test-admin')
    @td.with_wiki
    def test_wiki_stats(self):
        initial_artifacts = c.user.stats.getArtifacts()
        initial_wiki = c.user.stats.getArtifacts(art_type="Wiki")

        h.set_context('test', 'wiki', neighborhood='Projects')
        page = WM.Page(title="TestPage", text="some text")
        page.commit()

        artifacts = c.user.stats.getArtifacts()
        wiki = c.user.stats.getArtifacts(art_type="Wiki")

        assert artifacts['created'] == 1 + initial_artifacts['created']
        assert artifacts['modified'] == initial_artifacts['modified']
        assert wiki['created'] == 1 + initial_wiki['created']
        assert wiki['modified'] == initial_wiki['modified']

        page = WM.Page(title="TestPage2", text="some different text")
        page.commit()

        artifacts = c.user.stats.getArtifacts()
        wiki = c.user.stats.getArtifacts(art_type="Wiki")

        assert artifacts['created'] == 2 + initial_artifacts['created']
        assert artifacts['modified'] == initial_artifacts['modified']
        assert wiki['created'] == 2 + initial_wiki['created']
        assert wiki['modified'] == initial_wiki['modified']


        page.text="some modified text"
        page.commit()

        artifacts = c.user.stats.getArtifacts()
        wiki = c.user.stats.getArtifacts(art_type="Wiki")

        assert artifacts['created'] == 2 + initial_artifacts['created']
        assert artifacts['modified'] == 1 + initial_artifacts['modified']
        assert wiki['created'] == 2 + initial_wiki['created']
        assert wiki['modified'] == 1 + initial_wiki['modified']


    @td.with_user_project('test-admin')
    @td.with_tracker
    def test_tracker_stats(self):
        initial_tickets = c.user.stats.getTickets()
        initial_tickets_artifacts = c.user.stats.getArtifacts(art_type="Ticket")

        h.set_context('test', 'bugs', neighborhood='Projects')
        ticket = TM.Ticket(ticket_num=12, summary="test", assigned_to_id = c.user._id)
        ticket.commit()

        tickets = c.user.stats.getTickets()
        tickets_artifacts = c.user.stats.getArtifacts(art_type="Ticket")

        assert tickets['assigned'] == initial_tickets['assigned'] + 1
        assert tickets['solved'] == initial_tickets['solved']
        assert tickets['revoked'] == initial_tickets['revoked']
        assert tickets_artifacts['created'] == initial_tickets_artifacts['created'] + 1
        assert tickets_artifacts['modified'] == initial_tickets_artifacts['modified']

        ticket.status = 'closed'
        ticket.commit()

        tickets = c.user.stats.getTickets()
        tickets_artifacts = c.user.stats.getArtifacts(art_type="Ticket")

        assert tickets['assigned'] == initial_tickets['assigned'] + 1
        assert tickets['solved'] == initial_tickets['solved'] + 1
        assert tickets['revoked'] == initial_tickets['revoked']
        assert tickets_artifacts['created'] == initial_tickets_artifacts['created'] + 1
        assert tickets_artifacts['modified'] == initial_tickets_artifacts['modified'] + 1

        h.set_context('test', 'bugs', neighborhood='Projects')
        ticket = TM.Ticket(ticket_num=13, summary="test")
        ticket.commit()
        
        tickets = c.user.stats.getTickets()
        tickets_artifacts = c.user.stats.getArtifacts(art_type="Ticket")

        assert tickets['assigned'] == initial_tickets['assigned'] + 1
        assert tickets['solved'] == initial_tickets['solved'] + 1
        assert tickets['revoked'] == initial_tickets['revoked']
        assert tickets_artifacts['created'] == initial_tickets_artifacts['created'] + 2
        assert tickets_artifacts['modified'] == initial_tickets_artifacts['modified'] + 1

        ticket.assigned_to_id = c.user._id
        ticket.commit()

        tickets = c.user.stats.getTickets()
        tickets_artifacts = c.user.stats.getArtifacts(art_type="Ticket")
        
        assert tickets['assigned'] == initial_tickets['assigned'] + 2
        assert tickets['solved'] == initial_tickets['solved'] + 1
        assert tickets['revoked'] == initial_tickets['revoked']
        assert tickets_artifacts['created'] == initial_tickets_artifacts['created'] + 2
        assert tickets_artifacts['modified'] == initial_tickets_artifacts['modified'] + 2

        ticket.assigned_to_id = User.by_username('test-user')._id
        ticket.commit()

        tickets = c.user.stats.getTickets()
        tickets_artifacts = c.user.stats.getArtifacts(art_type="Ticket")
        
        assert tickets['assigned'] == initial_tickets['assigned'] + 2
        assert tickets['solved'] == initial_tickets['solved'] + 1
        assert tickets['revoked'] == initial_tickets['revoked'] + 1
        assert tickets_artifacts['created'] == initial_tickets_artifacts['created'] + 2
        assert tickets_artifacts['modified'] == initial_tickets_artifacts['modified'] + 3

class TestGitCommit(unittest.TestCase, TestController):

    def setUp(self):
        setup_basic_test()

        user = User.by_username('test-admin')
        user.set_password('testpassword')
        addr = M.EmailAddress.upsert('rcopeland@geek.net')
        user.claim_address('rcopeland@geek.net')
        self.setup_with_tools()

    @with_git
    @td.with_wiki
    def setup_with_tools(self):
        setup_global_objects()
        h.set_context('test', 'src-git', neighborhood='Projects')
        repo_dir = pkg_resources.resource_filename(
            'forgeuserstats', 'tests/data')
        c.app.repo.fs_path = repo_dir
        c.app.repo.name = 'testgit.git'
        self.repo = c.app.repo
        self.repo.refresh()
        self.rev = M.repo.Commit.query.get(_id=self.repo.heads[0]['object_id'])
        self.rev.repo = self.repo

    @td.with_user_project('test-admin')
    def test_commit(self):
        commits = c.user.stats.getCommits()
        assert commits['number'] == 4
        lmcommits = c.user.stats.getLastMonthCommits()
        assert lmcommits['number'] == 4

