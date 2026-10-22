"""Windows service lifetime locking with mocked contention and time."""
import errno
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

if sys.platform == "win32":
    import msvcrt
    from app.service_lock import service_lock


@unittest.skipUnless(sys.platform == "win32","Windows background-service lifetime lock")
class ServiceLifetimeLock(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name)/"server.lock"

    def test_old_server_releasing_its_lock_does_not_immediately_abort_a_new_start(self):
        busy = PermissionError(errno.EACCES,"synthetic previous owner")
        with patch("app.service_lock.msvcrt.locking",side_effect=[busy,busy,None,None]) as locking, \
             patch("app.service_lock.monotonic",side_effect=[0,0.1,0.2]), \
             patch("app.service_lock.sleep") as sleep,patch("builtins.print") as output:
            with service_lock(self.path):
                self.assertEqual(locking.call_count,3)
            self.assertEqual(sleep.call_args_list,[call(0.25),call(0.25)])
            output.assert_called_once()
            self.assertEqual(locking.call_args.args[1:],(msvcrt.LK_UNLCK,1))

    def test_busy_owner_times_out_without_entering_or_unlocking_its_service(self):
        with patch("app.service_lock.msvcrt.locking",side_effect=PermissionError(errno.EACCES,"busy")) as locking, \
             patch("app.service_lock.monotonic",side_effect=[0,30]),patch("app.service_lock.sleep") as sleep:
            with self.assertRaisesRegex(TimeoutError,"previous backend"):
                with service_lock(self.path):
                    self.fail("Must not run another server while its predecessor owns the lock")
            self.assertEqual(locking.call_count,1)
            self.assertEqual(locking.call_args.args[1:],(msvcrt.LK_NBLCK,1))
            sleep.assert_not_called()

    def test_unrelated_lock_error_is_not_hidden_as_a_shutdown_wait(self):
        failure = OSError(errno.EBADF,"synthetic invalid descriptor")
        with patch("app.service_lock.msvcrt.locking",side_effect=failure),patch("app.service_lock.sleep") as sleep:
            with self.assertRaises(OSError) as caught:
                with service_lock(self.path):
                    self.fail("Invalid descriptor must not start the server")
            self.assertIs(caught.exception,failure)
            sleep.assert_not_called()

    def test_service_exception_still_releases_the_lifetime_lock(self):
        with patch("app.service_lock.msvcrt.locking") as locking:
            with self.assertRaisesRegex(RuntimeError,"application failure"):
                with service_lock(self.path):
                    raise RuntimeError("application failure")
            self.assertEqual([entry.args[1:] for entry in locking.call_args_list],
                             [(msvcrt.LK_NBLCK,1),(msvcrt.LK_UNLCK,1)])
        self.assertEqual(self.path.read_bytes(),b"0")

    def test_repeated_starts_do_not_append_bytes_to_an_existing_lock_file(self):
        original = b"0"*36
        self.path.write_bytes(original)
        with patch("app.service_lock.msvcrt.locking"):
            for _ in range(3):
                with service_lock(self.path):
                    pass
        self.assertEqual(self.path.read_bytes(),original)
