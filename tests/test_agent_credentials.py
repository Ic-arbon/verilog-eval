from __future__ import annotations

import concurrent.futures
import os
import stat
import tempfile
import unittest
from pathlib import Path

from agent_generation.credentials import (
    CredentialBroker,
    CredentialError,
    request_credential,
)


SECRET = "broker-secret-canary"
DIGEST = "a" * 64
SAMPLES = ("Prob001_zero_sample01", "Prob002_one_sample01")


class CredentialBrokerTests(unittest.TestCase):
    def test_round_trip_validates_identity_and_unlinks_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / DIGEST
            run.mkdir(mode=0o700)
            with CredentialBroker(
                run_dir=run,
                config_digest=DIGEST,
                environment_name="OPENAI_API_KEY",
                secret=SECRET,
                expected_sample_ids=SAMPLES,
            ):
                socket_path = run / ".credential.sock"
                self.assertTrue(stat.S_ISSOCK(socket_path.lstat().st_mode))
                self.assertEqual(socket_path.stat().st_mode & 0o777, 0o600)
                value = request_credential(
                    run_dir=run,
                    config_digest=DIGEST,
                    sample_id=SAMPLES[0],
                    environment_name="OPENAI_API_KEY",
                )
                self.assertEqual(value, SECRET)
            self.assertFalse(socket_path.exists())

    def test_wrong_digest_sample_or_environment_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / DIGEST
            run.mkdir(mode=0o700)
            with CredentialBroker(
                run_dir=run,
                config_digest=DIGEST,
                environment_name="OPENAI_API_KEY",
                secret=SECRET,
                expected_sample_ids=SAMPLES,
            ):
                cases = (
                    ("b" * 64, SAMPLES[0], "OPENAI_API_KEY"),
                    (DIGEST, "Prob999_unknown_sample01", "OPENAI_API_KEY"),
                    (DIGEST, SAMPLES[0], "OTHER_KEY"),
                )
                for digest, sample, environment in cases:
                    with self.subTest(sample=sample), self.assertRaises(CredentialError):
                        request_credential(
                            run_dir=run,
                            config_digest=digest,
                            sample_id=sample,
                            environment_name=environment,
                        )

    def test_concurrent_requests_and_long_run_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root
            while len(str(run / ".credential.sock")) < 180:
                run /= "long-directory-segment"
            run /= DIGEST
            run.mkdir(parents=True, mode=0o700)
            with CredentialBroker(
                run_dir=run,
                config_digest=DIGEST,
                environment_name="OPENAI_API_KEY",
                secret=SECRET,
                expected_sample_ids=SAMPLES,
            ):
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    values = list(
                        pool.map(
                            lambda index: request_credential(
                                run_dir=run,
                                config_digest=DIGEST,
                                sample_id=SAMPLES[index % len(SAMPLES)],
                                environment_name="OPENAI_API_KEY",
                            ),
                            range(16),
                        )
                    )
            self.assertEqual(values, [SECRET] * 16)

    def test_forced_broker_death_is_cleaned_without_logging_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / DIGEST
            run.mkdir(mode=0o700)
            broker = CredentialBroker(
                run_dir=run,
                config_digest=DIGEST,
                environment_name="OPENAI_API_KEY",
                secret=SECRET,
                expected_sample_ids=SAMPLES,
            )
            broker.start()
            broker.terminate_for_test()
            broker.stop()

            self.assertFalse((run / ".credential.sock").exists())
            self.assertNotIn(SECRET, " ".join(os.environ.values()))


if __name__ == "__main__":
    unittest.main()
