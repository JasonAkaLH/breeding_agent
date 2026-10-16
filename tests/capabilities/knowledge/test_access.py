from __future__ import annotations

import unittest

from src.capabilities.knowledge import AccessSubjectResolver, KnowledgeError, LibraryAccessService


class AccessServiceTest(unittest.TestCase):
    def test_trusted_subject_resolver_can_extend_to_team_without_domain_dependency(self):
        class Repository:
            def get_library(self, library_id):
                return {"library_id": library_id, "owner_user_id": "owner", "status": "active"}

            def list_libraries(self):
                return [self.get_library("library")]

            def list_grants(self, _):
                # Future adapter fixture, not a grant supported by the current write API.
                return [{"subject_type": "team", "subject_id": "breeders", "role": "reader"}]

        class TrustedResolver(AccessSubjectResolver):
            def resolve_subjects(self, actor):
                subjects = super().resolve_subjects(actor)
                if actor == "member":
                    subjects.add(("team", "breeders"))
                return subjects

        access = LibraryAccessService(Repository(), TrustedResolver())
        self.assertEqual(access.require_permission("member", "library")["role"], "reader")
        self.assertEqual(len(access.list_accessible_libraries("member")), 1)
        with self.assertRaises(KnowledgeError):
            access.require_permission("member", "library", "write")
        with self.assertRaises(KnowledgeError):
            access.require_permission("outsider", "library")
        self.assertEqual(access.require_permission("owner", "library", "write")["role"], "owner")


if __name__ == "__main__":
    unittest.main()
