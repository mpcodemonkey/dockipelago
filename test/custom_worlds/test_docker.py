"""Tests for building and publishing the image a crawl produced.

Nothing here runs docker. What is worth testing is the decisions taken around it: that a push which
cannot possibly succeed is refused before a multi-minute build rather than after, and that the token
never reaches a place another user on the machine could read it.
"""

import unittest
from pathlib import Path
from unittest import mock

from tools.custom_worlds.docker import (
    DEFAULT_IMAGE,
    DEFAULT_TAG,
    TOKEN_ENV,
    USERNAME_ENV,
    DockerOptions,
    run,
)

ROOT = Path(".")


def credentials(user: str = "someone", secret: str = "s3cret") -> mock._patch_dict:
    return mock.patch.dict("os.environ", {USERNAME_ENV: user, TOKEN_ENV: secret}, clear=False)


def no_credentials() -> mock._patch_dict:
    environment = dict.fromkeys((USERNAME_ENV, TOKEN_ENV), "")
    return mock.patch.dict("os.environ", environment, clear=False)


class TestWhenItDoesNothing(unittest.TestCase):
    def test_neither_flag_means_no_work_and_no_complaint(self) -> None:
        with mock.patch("tools.custom_worlds.docker._build") as build:
            ok, detail = run(ROOT, DockerOptions())
        self.assertTrue(ok)
        self.assertEqual("", detail)
        self.assertFalse(build.called)


class TestBuilding(unittest.TestCase):
    def test_a_build_uses_the_image_and_tag_asked_for(self) -> None:
        with mock.patch("tools.custom_worlds.docker._build", return_value=True) as build:
            ok, detail = run(ROOT, DockerOptions(build=True, image="mine/thing", tag="test"))
        self.assertTrue(ok)
        self.assertIn("mine/thing:test", detail)
        build.assert_called_once_with(ROOT, "mine/thing:test")

    def test_the_defaults_need_no_namespace(self) -> None:
        # Building for yourself is the common case and should not demand a Docker Hub account.
        with no_credentials(), mock.patch("tools.custom_worlds.docker._build", return_value=True):
            ok, detail = run(ROOT, DockerOptions(build=True))
        self.assertTrue(ok)
        self.assertIn(f"{DEFAULT_IMAGE}:{DEFAULT_TAG}", detail)

    def test_a_failed_build_is_reported(self) -> None:
        with mock.patch("tools.custom_worlds.docker._build", return_value=False):
            ok, detail = run(ROOT, DockerOptions(build=True))
        self.assertFalse(ok)
        self.assertIn("could not build", detail)


class TestRefusingBeforeBuilding(unittest.TestCase):
    """A build takes minutes; finding out afterwards that it could never be pushed wastes them."""

    def attempt(self, options: DockerOptions) -> tuple[bool, str, bool]:
        with mock.patch("tools.custom_worlds.docker._build", return_value=True) as build:
            ok, detail = run(ROOT, options)
        return ok, detail, build.called

    def test_missing_credentials_stop_it_before_the_build(self) -> None:
        with no_credentials():
            ok, detail, built = self.attempt(DockerOptions(build=True, push=True, image="you/thing"))
        self.assertFalse(ok)
        self.assertFalse(built, "nothing should be built when the push cannot succeed")
        self.assertIn(USERNAME_ENV, detail)
        self.assertIn(TOKEN_ENV, detail)

    def test_it_names_only_the_credential_that_is_missing(self) -> None:
        with mock.patch.dict("os.environ", {USERNAME_ENV: "someone", TOKEN_ENV: ""}, clear=False):
            _ok, detail, _built = self.attempt(DockerOptions(build=True, push=True, image="you/thing"))
        self.assertIn(TOKEN_ENV, detail)
        self.assertNotIn(USERNAME_ENV, detail)

    def test_an_image_without_a_namespace_cannot_be_pushed(self) -> None:
        with credentials():
            ok, detail, built = self.attempt(DockerOptions(build=True, push=True, image="thing"))
        self.assertFalse(ok)
        self.assertFalse(built)
        self.assertIn("someone/thing", detail, "the message should show what to use instead")

    def test_the_refusal_points_at_building_without_publishing(self) -> None:
        with no_credentials():
            _ok, detail, _built = self.attempt(DockerOptions(build=True, push=True, image="you/thing"))
        self.assertIn("--docker-build", detail)


class TestPublishing(unittest.TestCase):
    def test_a_successful_push_is_reported(self) -> None:
        with credentials(), mock.patch.multiple(
            "tools.custom_worlds.docker",
            _build=mock.DEFAULT,
            _login=mock.DEFAULT,
            _push=mock.DEFAULT,
        ) as patched:
            for name in patched:
                patched[name].return_value = True
            ok, detail = run(ROOT, DockerOptions(build=True, push=True, image="you/thing"))
        self.assertTrue(ok)
        self.assertIn("pushed you/thing", detail)

    def test_the_token_is_never_passed_as_an_argument(self) -> None:
        """It would be readable in the process list and left in shell history."""
        with credentials(user="someone", secret="s3cret"), mock.patch(
            "tools.custom_worlds.docker.subprocess.run"
        ) as spawn:
            spawn.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch("tools.custom_worlds.docker._build", return_value=True), mock.patch(
                "tools.custom_worlds.docker._push", return_value=True
            ):
                run(ROOT, DockerOptions(build=True, push=True, image="you/thing"))

        spawn.assert_called_once()
        command = spawn.call_args.args[0]
        self.assertNotIn("s3cret", command)
        self.assertIn("--password-stdin", command)
        self.assertEqual("s3cret", spawn.call_args.kwargs["input"], "the token goes in on stdin")

    def test_a_failed_login_does_not_report_a_push(self) -> None:
        with credentials(), mock.patch(
            "tools.custom_worlds.docker._build", return_value=True
        ), mock.patch("tools.custom_worlds.docker._login", return_value=False), mock.patch(
            "tools.custom_worlds.docker._push"
        ) as push:
            ok, detail = run(ROOT, DockerOptions(build=True, push=True, image="you/thing"))
        self.assertFalse(ok)
        self.assertFalse(push.called)
        self.assertIn("could not log in", detail)

    def test_a_description_failure_does_not_lose_the_push(self) -> None:
        # The image is the point; the description is decoration.
        with credentials(), mock.patch.multiple(
            "tools.custom_worlds.docker", _build=mock.DEFAULT, _login=mock.DEFAULT, _push=mock.DEFAULT
        ) as patched:
            for name in patched:
                patched[name].return_value = True
            with mock.patch(
                "tools.custom_worlds.docker._describe", return_value=(False, "the description was not updated: nope")
            ):
                ok, detail = run(ROOT, DockerOptions(build=True, push=True, describe=True, image="you/thing"))
        self.assertTrue(ok, "a published image is still published")
        self.assertIn("pushed you/thing", detail)
        self.assertIn("not updated", detail)


if __name__ == "__main__":
    unittest.main()
