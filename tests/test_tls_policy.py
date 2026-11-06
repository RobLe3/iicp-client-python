"""SDK-05 constructor controls; no request or TLS handshake is performed."""
from unittest import TestCase, mock

from iicp_client import ClientConfig, IicpClient, IicpError


class TlsCertificatePolicyTests(TestCase):
    def test_default_verification_is_accepted(self):
        with mock.patch("httpx.AsyncClient", side_effect=AssertionError("unexpected HTTP client")):
            self.assertIsInstance(IicpClient(), IicpClient)

    def test_explicit_verification_is_accepted(self):
        with mock.patch("httpx.AsyncClient", side_effect=AssertionError("unexpected HTTP client")):
            self.assertIsInstance(IicpClient(ClientConfig(tls_verify=True)), IicpClient)

    def test_disabled_verification_is_refused_before_any_request(self):
        with mock.patch("httpx.AsyncClient", side_effect=AssertionError("unexpected HTTP client")):
            with self.assertRaises(IicpError) as caught:
                IicpClient(ClientConfig(tls_verify=False))
        self.assertEqual(caught.exception.code, "SDK-05")
        self.assertEqual(caught.exception.component, "sdk")
        self.assertFalse(caught.exception.retryable)
