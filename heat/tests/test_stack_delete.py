#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import copy
import time

from keystoneclient import exceptions as kc_exceptions
import mock
from oslo_config import cfg

from heat.common import exception
from heat.common import heat_keystoneclient as hkc
from heat.common import template_format
import heat.db.api as db_api
from heat.engine.clients.os import keystone
from heat.engine import resource
from heat.engine import scheduler
from heat.engine import stack
from heat.engine import template
from heat.tests import common
from heat.tests import fakes
from heat.tests import generic_resource as generic_rsrc
from heat.tests import utils

empty_template = template_format.parse('''{
  "HeatTemplateFormatVersion" : "2012-12-12",
}''')


class StackTest(common.HeatTestCase):
    def setUp(self):
        super(StackTest, self).setUp()

        self.tmpl = template.Template(copy.deepcopy(empty_template))
        self.ctx = utils.dummy_context()
        resource._register_class('GenericResourceType',
                                 generic_rsrc.GenericResource)

    def test_delete(self):
        self.stack = stack.Stack(self.ctx, 'delete_test', self.tmpl)
        stack_id = self.stack.store()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)

        self.stack.delete()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNone(db_s)
        self.assertEqual((stack.Stack.DELETE, stack.Stack.COMPLETE),
                         self.stack.state)

    def test_delete_user_creds(self):
        self.stack = stack.Stack(self.ctx, 'delete_test', self.tmpl)
        stack_id = self.stack.store()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)
        self.assertIsNotNone(db_s.user_creds_id)
        user_creds_id = db_s.user_creds_id
        db_creds = db_api.user_creds_get(db_s.user_creds_id)
        self.assertIsNotNone(db_creds)

        self.stack.delete()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNone(db_s)
        db_creds = db_api.user_creds_get(user_creds_id)
        self.assertIsNone(db_creds)
        del_db_s = db_api.stack_get(self.ctx, stack_id, show_deleted=True)
        self.assertIsNone(del_db_s.user_creds_id)
        self.assertEqual((stack.Stack.DELETE, stack.Stack.COMPLETE),
                         self.stack.state)

    def test_delete_user_creds_gone_missing(self):
        '''Do not block stack deletion if user_creds is missing.

        It may happen that user_creds were deleted when a delete operation was
        stopped. We should be resilient to this and still complete the delete
        operation.
        '''
        self.stack = stack.Stack(self.ctx, 'delete_test', self.tmpl)
        stack_id = self.stack.store()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)
        self.assertIsNotNone(db_s.user_creds_id)
        user_creds_id = db_s.user_creds_id
        db_creds = db_api.user_creds_get(db_s.user_creds_id)
        self.assertIsNotNone(db_creds)

        db_api.user_creds_delete(self.ctx, user_creds_id)

        self.stack.delete()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNone(db_s)
        db_creds = db_api.user_creds_get(user_creds_id)
        self.assertIsNone(db_creds)
        del_db_s = db_api.stack_get(self.ctx, stack_id, show_deleted=True)
        self.assertIsNone(del_db_s.user_creds_id)
        self.assertEqual((stack.Stack.DELETE, stack.Stack.COMPLETE),
                         self.stack.state)

    def test_delete_user_creds_fail(self):
        '''Do not stop deleting stacks even failed deleting user_creds.

        It may happen that user_creds were incorrectly saved (truncated) and
        thus cannot be correctly retrieved (and decrypted). In this case,
        stack delete should not be stopped.
        '''
        self.stack = stack.Stack(self.ctx, 'delete_test', self.tmpl)
        stack_id = self.stack.store()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)
        self.assertIsNotNone(db_s.user_creds_id)
        exc = exception.Error('Cannot get user credentials')
        self.patchobject(db_api, 'user_creds_get').side_effect = exc

        self.stack.delete()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNone(db_s)
        self.assertEqual((stack.Stack.DELETE, stack.Stack.COMPLETE),
                         self.stack.state)

    def test_delete_trust(self):
        cfg.CONF.set_override('deferred_auth_method', 'trusts')
        self.stub_keystoneclient()

        self.stack = stack.Stack(self.ctx, 'delete_trust', self.tmpl)
        stack_id = self.stack.store()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)

        self.stack.delete()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNone(db_s)
        self.assertEqual((stack.Stack.DELETE, stack.Stack.COMPLETE),
                         self.stack.state)

    def test_delete_trust_trustor(self):
        cfg.CONF.set_override('deferred_auth_method', 'trusts')

        trustor_ctx = utils.dummy_context(user_id='thetrustor')
        mock_kc = self.patchobject(hkc, 'KeystoneClient')
        mock_kc.return_value = fakes.FakeKeystoneClient(user_id='thetrustor')

        self.stack = stack.Stack(trustor_ctx, 'delete_trust_nt', self.tmpl)
        stack_id = self.stack.store()

        mock_kc.assert_called_once_with(trustor_ctx)

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)

        user_creds_id = db_s.user_creds_id
        self.assertIsNotNone(user_creds_id)
        user_creds = db_api.user_creds_get(user_creds_id)
        self.assertEqual('thetrustor', user_creds.get('trustor_user_id'))

        self.stack.delete()

        db_s = db_api.stack_get(trustor_ctx, stack_id)
        self.assertIsNone(db_s)
        self.assertEqual((stack.Stack.DELETE, stack.Stack.COMPLETE),
                         self.stack.state)

    def test_delete_trust_not_trustor(self):
        cfg.CONF.set_override('deferred_auth_method', 'trusts')

        # Stack gets created with trustor_ctx, deleted with other_ctx
        # then the trust delete should be with stored_ctx
        trustor_ctx = utils.dummy_context(user_id='thetrustor')
        other_ctx = utils.dummy_context(user_id='nottrustor')
        stored_ctx = utils.dummy_context(trust_id='thetrust')

        mock_kc = self.patchobject(hkc, 'KeystoneClient')
        mock_kc.return_value = fakes.FakeKeystoneClient(user_id='thetrustor')

        mock_sc = self.patchobject(stack.Stack, 'stored_context')
        mock_sc.return_value = stored_ctx

        self.stack = stack.Stack(trustor_ctx, 'delete_trust_nt', self.tmpl)
        stack_id = self.stack.store()
        mock_kc.assert_called_once_with(trustor_ctx)

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)

        user_creds_id = db_s.user_creds_id
        self.assertIsNotNone(user_creds_id)
        user_creds = db_api.user_creds_get(user_creds_id)
        self.assertEqual('thetrustor', user_creds.get('trustor_user_id'))

        mock_kc.return_value = fakes.FakeKeystoneClient(user_id='nottrustor')

        loaded_stack = stack.Stack.load(other_ctx, self.stack.id)
        loaded_stack.delete()
        mock_sc.assert_called_with()
        mock_kc.assert_has_calls([mock.call(trustor_ctx),
                                  mock.call(stored_ctx)])

        db_s = db_api.stack_get(other_ctx, stack_id)
        self.assertIsNone(db_s)
        self.assertEqual((stack.Stack.DELETE, stack.Stack.COMPLETE),
                         loaded_stack.state)

    def test_delete_trust_backup(self):
        cfg.CONF.set_override('deferred_auth_method', 'trusts')

        class FakeKeystoneClientFail(fakes.FakeKeystoneClient):
            def delete_trust(self, trust_id):
                raise Exception("Shouldn't delete")

        mock_kcp = self.patchobject(keystone.KeystoneClientPlugin, '_create',
                                    return_value=FakeKeystoneClientFail())

        self.stack = stack.Stack(self.ctx, 'delete_trust', self.tmpl)
        stack_id = self.stack.store()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)

        self.stack.delete(backup=True)

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNone(db_s)
        self.assertEqual(self.stack.state,
                         (stack.Stack.DELETE, stack.Stack.COMPLETE))
        mock_kcp.assert_called_once_with()

    def test_delete_trust_nested(self):
        cfg.CONF.set_override('deferred_auth_method', 'trusts')

        class FakeKeystoneClientFail(fakes.FakeKeystoneClient):
            def delete_trust(self, trust_id):
                raise Exception("Shouldn't delete")

        self.stub_keystoneclient(fake_client=FakeKeystoneClientFail())

        self.stack = stack.Stack(self.ctx, 'delete_trust_nested', self.tmpl,
                                 owner_id='owner123')
        stack_id = self.stack.store()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)
        user_creds_id = db_s.user_creds_id
        self.assertIsNotNone(user_creds_id)
        user_creds = db_api.user_creds_get(user_creds_id)
        self.assertIsNotNone(user_creds)

        self.stack.delete()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNone(db_s)
        user_creds = db_api.user_creds_get(user_creds_id)
        self.assertIsNotNone(user_creds)
        self.assertEqual((stack.Stack.DELETE, stack.Stack.COMPLETE),
                         self.stack.state)

    def test_delete_trust_fail(self):
        cfg.CONF.set_override('deferred_auth_method', 'trusts')

        class FakeKeystoneClientFail(fakes.FakeKeystoneClient):
            def delete_trust(self, trust_id):
                raise kc_exceptions.Forbidden("Denied!")

        mock_kcp = self.patchobject(keystone.KeystoneClientPlugin, '_create',
                                    return_value=FakeKeystoneClientFail())

        self.stack = stack.Stack(self.ctx, 'delete_trust', self.tmpl)
        stack_id = self.stack.store()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)

        self.stack.delete()

        mock_kcp.assert_called_with()
        self.assertEqual(2, mock_kcp.call_count)

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)
        self.assertEqual((stack.Stack.DELETE, stack.Stack.FAILED),
                         self.stack.state)
        self.assertIn('Error deleting trust', self.stack.status_reason)

    def test_delete_deletes_project(self):
        fkc = fakes.FakeKeystoneClient()
        fkc.delete_stack_domain_project = mock.Mock()

        mock_kcp = self.patchobject(keystone.KeystoneClientPlugin, '_create',
                                    return_value=fkc)

        self.stack = stack.Stack(self.ctx, 'delete_trust', self.tmpl)
        stack_id = self.stack.store()

        self.stack.set_stack_user_project_id(project_id='aproject456')

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)

        self.stack.delete()

        mock_kcp.assert_called_once_with()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNone(db_s)
        self.assertEqual((stack.Stack.DELETE, stack.Stack.COMPLETE),
                         self.stack.state)
        fkc.delete_stack_domain_project.assert_called_once_with(
            project_id='aproject456')

    def test_delete_rollback(self):
        self.stack = stack.Stack(self.ctx, 'delete_rollback_test',
                                 self.tmpl, disable_rollback=False)
        stack_id = self.stack.store()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)

        self.stack.delete(action=self.stack.ROLLBACK)

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNone(db_s)
        self.assertEqual((stack.Stack.ROLLBACK, stack.Stack.COMPLETE),
                         self.stack.state)

    def test_delete_badaction(self):
        self.stack = stack.Stack(self.ctx, 'delete_badaction_test', self.tmpl)
        stack_id = self.stack.store()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)

        self.stack.delete(action="wibble")

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)
        self.assertEqual((stack.Stack.DELETE, stack.Stack.FAILED),
                         self.stack.state)

    def test_stack_delete_timeout(self):
        self.stack = stack.Stack(self.ctx, 'delete_test', self.tmpl)
        stack_id = self.stack.store()

        db_s = db_api.stack_get(self.ctx, stack_id)
        self.assertIsNotNone(db_s)

        def dummy_task():
            while True:
                yield

        start_time = time.time()
        mock_tg = self.patchobject(scheduler.DependencyTaskGroup, '__call__',
                                   return_value=dummy_task())
        mock_wallclock = self.patchobject(scheduler, 'wallclock')
        mock_wallclock.side_effect = [
            start_time,
            start_time + 1,
            start_time + self.stack.timeout_secs() + 1
        ]

        self.stack.delete()

        self.assertEqual((stack.Stack.DELETE, stack.Stack.FAILED),
                         self.stack.state)
        self.assertEqual('Delete timed out', self.stack.status_reason)

        mock_tg.assert_called_once_with()
        mock_wallclock.assert_called_with()
        self.assertEqual(3, mock_wallclock.call_count)

    def test_stack_delete_resourcefailure(self):
        tmpl = {'HeatTemplateFormatVersion': '2012-12-12',
                'Resources': {'AResource': {'Type': 'GenericResourceType'}}}

        mock_rd = self.patchobject(generic_rsrc.GenericResource,
                                   'handle_delete',
                                   side_effect=Exception('foo'))

        self.stack = stack.Stack(self.ctx, 'delete_test_fail',
                                 template.Template(tmpl))

        self.stack.store()
        self.stack.create()
        self.assertEqual((self.stack.CREATE, self.stack.COMPLETE),
                         self.stack.state)

        self.stack.delete()

        mock_rd.assert_called_once()
        self.assertEqual((self.stack.DELETE, self.stack.FAILED),
                         self.stack.state)
        self.assertEqual('Resource DELETE failed: Exception: foo',
                         self.stack.status_reason)

    def test_stack_user_project_id_delete_fail(self):

        class FakeKeystoneClientFail(fakes.FakeKeystoneClient):
            def delete_stack_domain_project(self, project_id):
                raise kc_exceptions.Forbidden("Denied!")

        mock_kcp = self.patchobject(keystone.KeystoneClientPlugin, '_create',
                                    return_value=FakeKeystoneClientFail())

        self.stack = stack.Stack(self.ctx, 'user_project_init',
                                 self.tmpl,
                                 stack_user_project_id='aproject1234')
        self.stack.store()
        self.assertEqual('aproject1234', self.stack.stack_user_project_id)
        db_stack = db_api.stack_get(self.ctx, self.stack.id)
        self.assertEqual('aproject1234', db_stack.stack_user_project_id)

        self.stack.delete()

        mock_kcp.assert_called_once_with()
        self.assertEqual((stack.Stack.DELETE, stack.Stack.FAILED),
                         self.stack.state)
        self.assertIn('Error deleting project', self.stack.status_reason)
