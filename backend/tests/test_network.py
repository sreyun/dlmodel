import ssl

import httpx
import pytest

from app.downloaders.network import is_transient_network_error, retry_backoff_seconds


def test_retry_backoff_grows_and_caps():
    assert retry_backoff_seconds(0) == 1.5
    assert retry_backoff_seconds(1) == 3.0
    assert retry_backoff_seconds(10) == 30.0


def test_ssl_eof_is_transient():
    exc = ssl.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")
    assert is_transient_network_error(exc) is True


def test_httpx_read_error_is_transient():
    assert is_transient_network_error(httpx.ReadError("stream closed")) is True
    assert is_transient_network_error(httpx.ConnectError("boom")) is True
    assert is_transient_network_error(httpx.RemoteProtocolError("peer closed")) is True


def test_http_status_retryable():
    req = httpx.Request("GET", "https://example.com")
    resp = httpx.Response(503, request=req)
    assert is_transient_network_error(httpx.HTTPStatusError("no", request=req, response=resp))


def test_non_transient_value_error():
    assert is_transient_network_error(ValueError("bad path")) is False
