import unittest
from context_contract.operator import OperatorContextService, Principal

class FakeStore:
    def read(self,*args): return args
    def write(self,*args): return args
    def provision(self,*args): return args
    def history(self,*args): return args

class OperatorTests(unittest.TestCase):
    def setUp(self): self.service=OperatorContextService(FakeStore())
    def test_rejects_unauthenticated(self):
        with self.assertRaises(PermissionError):
            self.service.read(None,"sandbox","x","MODEL.md")
    def test_enforces_permissions(self):
        p=Principal("agent:a",frozenset({"context:read"}))
        with self.assertRaises(PermissionError):
            self.service.update(p,"sandbox","x","MODEL.md","",0)
    def test_actor_is_derived_from_principal(self):
        p=Principal("operator:1",frozenset({"context:write"}))
        result=self.service.update(p,"sandbox","x","MODEL.md","hello",0)
        self.assertEqual(result[-2],"operator:1")
    def test_sandbox_provisioning(self):
        p=Principal("operator:1",frozenset({"context:provision"}))
        self.assertEqual(self.service.provision_sandbox(p,"abc"),
                         ("sandbox","abc","operator:1"))
