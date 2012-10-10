from datetime import datetime
from collections import defaultdict
import unittest
import mock
from nose.tools import assert_equal
from pylons import c
from bson import ObjectId
from ming.orm import session

from alluratest.controller import setup_basic_test, setup_global_objects
from allura import model as M

class TestGitLikeTree(object):

    def test_set_blob(self):
        tree = M.GitLikeTree()
        tree.set_blob('/dir/dir2/file', 'file-oid')

        assert_equal(tree.blobs, {})
        assert_equal(tree.get_tree('dir').blobs, {})
        assert_equal(tree.get_tree('dir').get_tree('dir2').blobs, {'file': 'file-oid'})

    def test_hex(self):
        tree = M.GitLikeTree()
        tree.set_blob('/dir/dir2/file', 'file-oid')
        hex = tree.hex()

        # check the reprs. In case hex (below) fails, this'll be useful
        assert_equal(repr(tree.get_tree('dir').get_tree('dir2')), 'b file-oid file')
        assert_equal(repr(tree), 't 96af1772ecce1e6044e6925e595d9373ffcd2615 dir')
        # the hex() value shouldn't change, it's an important key
        assert_equal(hex, '4abba29a43411b9b7cecc1a74f0b27920554350d')

        # another one should be the same
        tree2 = M.GitLikeTree()
        tree2.set_blob('/dir/dir2/file', 'file-oid')
        hex2 = tree2.hex()
        assert_equal(hex, hex2)


class RepoImplTestBase(object):

    def test_commit_run(self):
        M.repo.CommitRunDoc.m.remove()
        commit_ids = list(self.repo.all_commit_ids())
        # simulate building up a commit run from multiple pushes
        for c_id in commit_ids:
            crb = M.repo_refresh.CommitRunBuilder([c_id])
            crb.run()
            crb.cleanup()
        runs = M.repo.CommitRunDoc.m.find().all()
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.commit_ids, commit_ids)
        self.assertEqual(len(run.commit_ids), len(run.commit_times))
        self.assertEqual(run.parent_commit_ids, [])

    def test_repair_commit_run(self):
        commit_ids = list(self.repo.all_commit_ids())
        # simulate building up a commit run from multiple pushes, but skip the
        # last commit to simulate a broken commit run
        for c_id in commit_ids[:-1]:
            crb = M.repo_refresh.CommitRunBuilder([c_id])
            crb.run()
            crb.cleanup()
        # now repair the commitrun by rebuilding with all commit ids
        crb = M.repo_refresh.CommitRunBuilder(commit_ids)
        crb.run()
        crb.cleanup()
        runs = M.repo.CommitRunDoc.m.find().all()
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.commit_ids, commit_ids)
        self.assertEqual(len(run.commit_ids), len(run.commit_times))
        self.assertEqual(run.parent_commit_ids, [])


class TestLastCommit(unittest.TestCase):
    def setUp(self):
        setup_basic_test()
        setup_global_objects()
        c.model_cache = M.repo.ModelCache()
        self.repo = mock.Mock('repo', _commits={}, _last_commit=None)
        self.repo.shorthand_for_commit = lambda _id: _id[:6]

    def _build_tree(self, commit, path, tree_paths):
        tree_nodes = []
        blob_nodes = []
        sub_paths = defaultdict(list)
        def n(p):
            m = mock.Mock()
            m.name = p
            return m
        for p in tree_paths:
            if '/' in p:
                node, sub = p.split('/',1)
                tree_nodes.append(n(node))
                sub_paths[node].append(sub)
            else:
                blob_nodes.append(n(p))
        tree = mock.Mock(
                commit=commit,
                path=mock.Mock(return_value=path),
                tree_ids=tree_nodes,
                blob_ids=blob_nodes,
                other_ids=[],
            )
        tree.get_obj_by_path = lambda p: self._build_tree(commit, p, sub_paths[p])
        return tree

    def _add_commit(self, msg, tree_paths, diff_paths=None, parents=[]):
        suser = dict(
                name='test',
                email='test@example.com',
                date=datetime(2013, 1, 1 + len(self.repo._commits)),
            )
        commit = M.repo.Commit(
                _id=str(ObjectId()),
                message=msg,
                parent_ids=[parent._id for parent in parents],
                commited=suser,
                authored=suser,
                repo=self.repo,
            )
        commit.tree = self._build_tree(commit, '/', tree_paths)
        diffinfo = M.repo.DiffInfoDoc(dict(
                _id=commit._id,
                differences=[{'name': p} for p in diff_paths or tree_paths],
            ))
        diffinfo.m.save()
        self.repo._commits[commit._id] = commit
        return commit

    def test_single_commit(self):
        commit1 = self._add_commit('Commit 1', [
                'file1',
                'dir1/file2',
            ])
        lcd = M.repo.LastCommit.get(commit1.tree)
        self.assertEqual([self.repo._commits[c].message for c in lcd.commit_ids], [commit1.message])
        self.assertEqual(lcd.path, '')
        self.assertEqual(len(lcd.entries), 2)
        self.assertEqual(lcd.entry_by_name('file1'), dict(
            type='BLOB',
            name='file1',
            commit_info=dict(
                summary='Commit 1',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 1),
                author_url=None,
                id=commit1._id,
                shortlink=self.repo.shorthand_for_commit(commit1._id),
            )))
        self.assertEqual(lcd.entry_by_name('dir1'), dict(
            type='DIR',
            name='dir1',
            commit_info=dict(
                summary='Commit 1',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 1),
                author_url=None,
                id=commit1._id,
                shortlink=self.repo.shorthand_for_commit(commit1._id),
            )))

    def test_multiple_commits_no_overlap(self):
        commit1 = self._add_commit('Commit 1', ['file1'])
        commit2 = self._add_commit('Commit 2', ['file1', 'dir1/file1'], ['dir1/file1'], [commit1])
        commit3 = self._add_commit('Commit 3', ['file1', 'dir1/file1', 'file2'], ['file2'], [commit2])
        lcd = M.repo.LastCommit.get(commit3.tree)
        self.assertEqual([self.repo._commits[c].message for c in lcd.commit_ids], [commit3.message])
        self.assertEqual(lcd.commit_ids, [commit3._id])
        self.assertEqual(lcd.path, '')
        self.assertEqual(len(lcd.entries), 3)
        self.assertEqual(lcd.entry_by_name('file1'), dict(
            type='BLOB',
            name='file1',
            commit_info=dict(
                summary='Commit 1',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 1),
                author_url=None,
                id=commit1._id,
                shortlink=self.repo.shorthand_for_commit(commit1._id),
            )))
        self.assertEqual(lcd.entry_by_name('dir1'), dict(
            type='DIR',
            name='dir1',
            commit_info=dict(
                summary='Commit 2',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 2),
                author_url=None,
                id=commit2._id,
                shortlink=self.repo.shorthand_for_commit(commit2._id),
            )))
        self.assertEqual(lcd.entry_by_name('file2'), dict(
            type='BLOB',
            name='file2',
            commit_info=dict(
                summary='Commit 3',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 3),
                author_url=None,
                id=commit3._id,
                shortlink=self.repo.shorthand_for_commit(commit3._id),
            )))

    def test_multiple_commits_with_overlap(self):
        commit1 = self._add_commit('Commit 1', ['file1'])
        commit2 = self._add_commit('Commit 2', ['file1', 'dir1/file1'], ['dir1/file1'], [commit1])
        commit3 = self._add_commit('Commit 3', ['file1', 'dir1/file1', 'file2'], ['file1', 'file2'], [commit2])
        lcd = M.repo.LastCommit.get(commit3.tree)
        self.assertEqual([self.repo._commits[c].message for c in lcd.commit_ids], [commit3.message])
        self.assertEqual(lcd.path, '')
        self.assertEqual(len(lcd.entries), 3)
        self.assertEqual(lcd.entry_by_name('file1'), dict(
            type='BLOB',
            name='file1',
            commit_info=dict(
                summary='Commit 3',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 3),
                author_url=None,
                id=commit3._id,
                shortlink=self.repo.shorthand_for_commit(commit3._id),
            )))
        self.assertEqual(lcd.entry_by_name('dir1'), dict(
            type='DIR',
            name='dir1',
            commit_info=dict(
                summary='Commit 2',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 2),
                author_url=None,
                id=commit2._id,
                shortlink=self.repo.shorthand_for_commit(commit2._id),
            )))
        self.assertEqual(lcd.entry_by_name('file2'), dict(
            type='BLOB',
            name='file2',
            commit_info=dict(
                summary='Commit 3',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 3),
                author_url=None,
                id=commit3._id,
                shortlink=self.repo.shorthand_for_commit(commit3._id),
            )))

    def test_multiple_commits_subdir_change(self):
        commit1 = self._add_commit('Commit 1', ['file1', 'dir1/file1'])
        commit2 = self._add_commit('Commit 2', ['file1', 'dir1/file1', 'dir1/file2'], ['dir1/file2'], [commit1])
        commit3 = self._add_commit('Commit 3', ['file1', 'dir1/file1', 'dir1/file2'], ['dir1/file1'], [commit2])
        lcd = M.repo.LastCommit.get(commit3.tree)
        self.assertEqual([self.repo._commits[c].message for c in lcd.commit_ids], [commit3.message])
        self.assertEqual(lcd.path, '')
        self.assertEqual(len(lcd.entries), 2)
        self.assertEqual(lcd.entry_by_name('file1'), dict(
            type='BLOB',
            name='file1',
            commit_info=dict(
                summary='Commit 1',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 1),
                author_url=None,
                id=commit1._id,
                shortlink=self.repo.shorthand_for_commit(commit1._id),
            )))
        self.assertEqual(lcd.entry_by_name('dir1'), dict(
            type='DIR',
            name='dir1',
            commit_info=dict(
                summary='Commit 3',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 3),
                author_url=None,
                id=commit3._id,
                shortlink=self.repo.shorthand_for_commit(commit3._id),
            )))

    def test_subdir_lcd(self):
        commit1 = self._add_commit('Commit 1', ['file1', 'dir1/file1'])
        commit2 = self._add_commit('Commit 2', ['file1', 'dir1/file1', 'dir1/file2'], ['dir1/file2'], [commit1])
        commit3 = self._add_commit('Commit 3', ['file1', 'dir1/file1', 'dir1/file2'], ['dir1/file1'], [commit2])
        tree = self._build_tree(commit3, '/dir1', ['file1', 'file2'])
        lcd = M.repo.LastCommit.get(tree)
        self.assertEqual([self.repo._commits[c].message for c in lcd.commit_ids], [commit3.message])
        self.assertEqual(lcd.path, 'dir1')
        self.assertEqual(len(lcd.entries), 2)
        self.assertEqual(lcd.entry_by_name('file1'), dict(
            type='BLOB',
            name='file1',
            commit_info=dict(
                summary='Commit 3',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 3),
                author_url=None,
                id=commit3._id,
                shortlink=self.repo.shorthand_for_commit(commit3._id),
            )))
        self.assertEqual(lcd.entry_by_name('file2'), dict(
            type='BLOB',
            name='file2',
            commit_info=dict(
                summary='Commit 2',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 2),
                author_url=None,
                id=commit2._id,
                shortlink=self.repo.shorthand_for_commit(commit2._id),
            )))

    def test_subdir_lcd_prev_commit(self):
        commit1 = self._add_commit('Commit 1', ['file1', 'dir1/file1'])
        commit2 = self._add_commit('Commit 2', ['file1', 'dir1/file1', 'dir1/file2'], ['dir1/file2'], [commit1])
        commit3 = self._add_commit('Commit 3', ['file1', 'dir1/file1', 'dir1/file2'], ['dir1/file1'], [commit2])
        commit4 = self._add_commit('Commit 4', ['file1', 'dir1/file1', 'dir1/file2', 'file2'], ['file2'], [commit3])
        tree = self._build_tree(commit4, '/dir1', ['file1', 'file2'])
        lcd = M.repo.LastCommit.get(tree)
        self.assertEqual([self.repo._commits[c].message for c in lcd.commit_ids], [commit4.message, commit3.message])
        self.assertEqual(lcd.path, 'dir1')
        self.assertEqual(len(lcd.entries), 2)
        self.assertEqual(lcd.entry_by_name('file1'), dict(
            type='BLOB',
            name='file1',
            commit_info=dict(
                summary='Commit 3',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 3),
                author_url=None,
                id=commit3._id,
                shortlink=self.repo.shorthand_for_commit(commit3._id),
            )))
        self.assertEqual(lcd.entry_by_name('file2'), dict(
            type='BLOB',
            name='file2',
            commit_info=dict(
                summary='Commit 2',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 2),
                author_url=None,
                id=commit2._id,
                shortlink=self.repo.shorthand_for_commit(commit2._id),
            )))

    def test_subdir_lcd_always_empty(self):
        commit1 = self._add_commit('Commit 1', ['file1', 'dir1'])
        commit2 = self._add_commit('Commit 2', ['file1', 'file2'], ['file2'], [commit1])
        tree = self._build_tree(commit2, '/dir1', [])
        lcd = M.repo.LastCommit.get(tree)
        self.assertEqual([self.repo._commits[c].message for c in lcd.commit_ids], [commit2.message, commit1.message])
        self.assertEqual(lcd.path, 'dir1')
        self.assertEqual(lcd.entries, [])

    def test_subdir_lcd_emptied(self):
        commit1 = self._add_commit('Commit 1', ['file1', 'dir1/file1'])
        commit2 = self._add_commit('Commit 2', ['file1'], ['dir1/file1'], [commit1])
        tree = self._build_tree(commit2, '/dir1', [])
        lcd = M.repo.LastCommit.get(tree)
        self.assertEqual([self.repo._commits[c].message for c in lcd.commit_ids], [commit2.message])
        self.assertEqual(lcd.path, 'dir1')
        self.assertEqual(lcd.entries, [])

    def test_existing_lcd_unchained(self):
        commit1 = self._add_commit('Commit 1', ['file1', 'dir1/file1'])
        commit2 = self._add_commit('Commit 2', ['file1', 'dir1/file1', 'dir1/file2'], ['dir1/file2'], [commit1])
        commit3 = self._add_commit('Commit 3', ['file1', 'dir1/file1', 'dir1/file2'], ['file1'], [commit2])
        prev_lcd = M.repo.LastCommit(
                path='dir1',
                commit_ids=[commit2._id],
                entries=[
                    dict(
                        type='BLOB',
                        name='file1',
                        commit_info=dict(
                            summary='Commit 1',
                            author='test',
                            author_email='test@example.com',
                            date=datetime(2013, 1, 1),
                            author_url=None,
                            id=commit1._id,
                            shortlink=self.repo.shorthand_for_commit(commit1._id),
                        )),
                    dict(
                        type='BLOB',
                        name='file2',
                        commit_info=dict(
                            summary='Commit 2',
                            author='test',
                            author_email='test@example.com',
                            date=datetime(2013, 1, 2),
                            author_url=None,
                            id=commit2._id,
                            shortlink=self.repo.shorthand_for_commit(commit2._id),
                        )),
                ],
            )
        session(prev_lcd).flush()
        tree = self._build_tree(commit3, '/dir1', ['file1', 'file2'])
        lcd = M.repo.LastCommit.get(tree)
        self.assertEqual(lcd._id, prev_lcd._id)
        self.assertEqual([self.repo._commits[c].message for c in lcd.commit_ids], [commit2.message, commit3.message])
        self.assertEqual(lcd.path, 'dir1')
        self.assertEqual(lcd.entries, prev_lcd.entries)

    def test_existing_lcd_partial(self):
        commit1 = self._add_commit('Commit 1', ['file1'])
        commit2 = self._add_commit('Commit 2', ['file1', 'file2'], ['file2'], [commit1])
        commit3 = self._add_commit('Commit 3', ['file1', 'file2', 'file3'], ['file3'], [commit2])
        commit4 = self._add_commit('Commit 4', ['file1', 'file2', 'file3', 'file4'], ['file2', 'file4'], [commit3])
        prev_lcd = M.repo.LastCommit(
                path='',
                commit_ids=[commit3._id],
                entries=[
                    dict(
                        type='BLOB',
                        name='file1',
                        commit_info=dict(
                            summary='Existing LCD',    # lying here to test that it uses this
                            author='test',             # data instead of walking up the tree
                            author_email='test@example.com',
                            date=datetime(2013, 1, 1),
                            author_url=None,
                            id=commit1._id,
                            shortlink=self.repo.shorthand_for_commit(commit1._id),
                        )),
                    dict(
                        type='BLOB',
                        name='file2',
                        commit_info=dict(
                            summary='Commit 2',
                            author='test',
                            author_email='test@example.com',
                            date=datetime(2013, 1, 2),
                            author_url=None,
                            id=commit2._id,
                            shortlink=self.repo.shorthand_for_commit(commit2._id),
                        )),
                    dict(
                        type='BLOB',
                        name='file3',
                        commit_info=dict(
                            summary='Commit 3',
                            author='test',
                            author_email='test@example.com',
                            date=datetime(2013, 1, 3),
                            author_url=None,
                            id=commit3._id,
                            shortlink=self.repo.shorthand_for_commit(commit3._id),
                        )),
                ],
            )
        session(prev_lcd).flush()
        lcd = M.repo.LastCommit.get(commit4.tree)
        self.assertEqual([self.repo._commits[c].message for c in lcd.commit_ids], [commit4.message])
        self.assertEqual(lcd.path, '')
        self.assertEqual(lcd.entry_by_name('file1')['commit_info']['summary'], 'Existing LCD')
        self.assertEqual(len(lcd.entries), 4)
        self.assertEqual(lcd.entry_by_name('file1'), dict(
            type='BLOB',
            name='file1',
            commit_info=dict(
                summary='Existing LCD',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 1),
                author_url=None,
                id=commit1._id,
                shortlink=self.repo.shorthand_for_commit(commit1._id),
            )))
        self.assertEqual(lcd.entry_by_name('file2'), dict(
            type='BLOB',
            name='file2',
            commit_info=dict(
                summary='Commit 4',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 4),
                author_url=None,
                id=commit4._id,
                shortlink=self.repo.shorthand_for_commit(commit4._id),
            )))
        self.assertEqual(lcd.entry_by_name('file3'), dict(
            type='BLOB',
            name='file3',
            commit_info=dict(
                summary='Commit 3',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 3),
                author_url=None,
                id=commit3._id,
                shortlink=self.repo.shorthand_for_commit(commit3._id),
            )))
        self.assertEqual(lcd.entry_by_name('file4'), dict(
            type='BLOB',
            name='file4',
            commit_info=dict(
                summary='Commit 4',
                author='test',
                author_email='test@example.com',
                date=datetime(2013, 1, 4),
                author_url=None,
                id=commit4._id,
                shortlink=self.repo.shorthand_for_commit(commit4._id),
            )))


class TestModelCache(unittest.TestCase):
    def setUp(self):
        self.cache = M.repo.ModelCache()

    def test_normalize_key(self):
        self.assertEqual(self.cache._normalize_key({'foo': 1, 'bar': 2}), (('bar', 2), ('foo', 1)))

    @mock.patch.object(M.repo.Tree.query, 'get')
    @mock.patch.object(M.repo.LastCommit.query, 'get')
    def test_get(self, lc_get, tr_get):
        tr_get.return_value = 'bar'
        lc_get.return_value = 'qux'

        val = self.cache.get(M.repo.Tree, {'_id': 'foo'})
        tr_get.assert_called_with(_id='foo')
        self.assertEqual(val, 'bar')

        val = self.cache.get(M.repo.LastCommit, {'_id': 'foo'})
        lc_get.assert_called_with(_id='foo')
        self.assertEqual(val, 'qux')

    @mock.patch.object(M.repo.Tree.query, 'get')
    def test_get_no_dup(self, tr_get):
        tr_get.return_value = 'bar'
        val = self.cache.get(M.repo.Tree, {'_id': 'foo'})
        tr_get.assert_called_once_with(_id='foo')
        self.assertEqual(val, 'bar')

        tr_get.return_value = 'qux'
        val = self.cache.get(M.repo.Tree, {'_id': 'foo'})
        tr_get.assert_called_once_with(_id='foo')
        self.assertEqual(val, 'bar')

    @mock.patch.object(M.repo.TreesDoc.m, 'get')
    def test_get_doc(self, tr_get):
        tr_get.return_value = 'bar'
        val = self.cache.get(M.repo.TreesDoc, {'_id': 'foo'})
        tr_get.assert_called_once_with(_id='foo')
        self.assertEqual(val, 'bar')

    def test_set(self):
        self.cache.set(M.repo.Tree, {'_id': 'foo'}, 'test_set')
        self.assertEqual(self.cache._cache, {M.repo.Tree: {(('_id', 'foo'),): 'test_set'}})

    def test_keys(self):
        self.cache._cache[M.repo.Tree][(('_id', 'test_keys'), ('text', 'tko'))] = 'foo'
        self.cache._cache[M.repo.Tree][(('fubar', 'scm'),)] = 'bar'
        self.assertEqual(self.cache.keys(M.repo.Tree), [{'_id': 'test_keys', 'text': 'tko'}, {'fubar': 'scm'}])
        self.assertEqual(self.cache.keys(M.repo.LastCommit), [])

    @mock.patch.object(M.repo.Tree.query, 'find')
    def test_batch_load(self, tr_find):
        # cls, query, attrs
        m1 = mock.Mock(foo=1, qux=3)
        m2 = mock.Mock(foo=2, qux=5)
        tr_find.return_value = [m1, m2]

        self.cache.batch_load(M.repo.Tree, {'foo': {'$in': 'bar'}})
        tr_find.assert_called_with({'foo': {'$in': 'bar'}})
        self.assertEqual(self.cache._cache[M.repo.Tree], {
                (('foo', 1),): m1,
                (('foo', 2),): m2,
            })

    @mock.patch.object(M.repo.Tree.query, 'find')
    def test_batch_load_attrs(self, tr_find):
        # cls, query, attrs
        m1 = mock.Mock(foo=1, qux=3)
        m2 = mock.Mock(foo=2, qux=5)
        tr_find.return_value = [m1, m2]

        self.cache.batch_load(M.repo.Tree, {'foo': {'$in': 'bar'}}, ['qux'])
        tr_find.assert_called_with({'foo': {'$in': 'bar'}})
        self.assertEqual(self.cache._cache[M.repo.Tree], {
                (('qux', 3),): m1,
                (('qux', 5),): m2,
            })

    def test_pruning(self):
        self.cache.max_size = 2
        self.cache.set(M.repo.Tree, {'_id': 'foo'}, 'bar')
        self.cache.set(M.repo.Tree, {'_id': 'qux'}, 'zaz')
        self.cache.set(M.repo.Tree, {'_id': 'f00'}, 'b4r')
        self.cache.set(M.repo.Tree, {'_id': 'qux'}, 'zaz')
        self.assertEqual(self.cache._cache, {
                M.repo.Tree: {
                    (('_id', 'qux'),): 'zaz',
                    (('_id', 'f00'),): 'b4r',
                },
            })
