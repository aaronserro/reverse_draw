"""Run a bounded authenticated marketplace polling load profile.

Use either a local JSON array of objects with ``name`` and ``code`` or the
interactive repeat-user mode. Never commit credentials. Results contain only
aggregate timings and statuses.
"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import math
import random
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


class Results:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latencies_ms: list[float] = []
        self.latencies_by_status: dict[int, list[float]] = {}
        self.statuses: Counter[int] = Counter()
        self.login_failures = 0
        self.conditional_requests = 0
        self.etag_responses = 0

    def record(
        self,
        status: int,
        duration_ms: float,
        *,
        conditional: bool,
        received_etag: bool,
    ) -> None:
        with self.lock:
            self.statuses[status] += 1
            self.latencies_ms.append(duration_ms)
            self.latencies_by_status.setdefault(status, []).append(duration_ms)
            self.conditional_requests += int(conditional)
            self.etag_responses += int(received_etag)

    def login_failed(self) -> None:
        with self.lock:
            self.login_failures += 1


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(
        0,
        min(len(ordered) - 1, math.ceil(percentile_value * len(ordered)) - 1),
    )
    return ordered[index]


def latency_summary(values: list[float]) -> dict[str, float | int]:
    return {
        "requests": len(values),
        "mean": round(statistics.fmean(values), 2) if values else 0,
        "p50": round(percentile(values, 0.50), 2),
        "p95": round(percentile(values, 0.95), 2),
        "p99": round(percentile(values, 0.99), 2),
        "maximum": round(max(values), 2) if values else 0,
    }


def json_request(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
    data = json.dumps(body).encode() if body is not None else None
    request_headers = dict(headers or {})
    if data is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            payload = json.loads(raw) if raw else None
            response_headers = {
                key.casefold(): value
                for key, value in response.headers.items()
            }
            return response.status, payload, response_headers
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = None
        response_headers = {
            key.casefold(): value for key, value in error.headers.items()
        }
        return error.code, payload, response_headers


def run_user(
    index: int,
    user: dict[str, str],
    *,
    base_url: str,
    duration_seconds: float,
    ramp_seconds: float,
    poll_seconds: float,
    user_count: int,
    results: Results,
) -> None:
    time.sleep(ramp_seconds * index / max(1, user_count))
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )
    status, payload, _ = json_request(
        opener,
        f"{base_url}/api/trading/login",
        method="POST",
        body={"name": user["name"], "code": user["code"]},
    )
    if status != 200 or not payload or not payload.get("authenticated"):
        results.login_failed()
        return

    deadline = time.monotonic() + duration_seconds
    etag = ""
    while time.monotonic() < deadline:
        started = time.perf_counter()
        headers = {"If-None-Match": etag} if etag else {}
        status, _, response_headers = json_request(
            opener,
            f"{base_url}/api/trading/market",
            headers=headers,
        )
        results.record(
            status,
            (time.perf_counter() - started) * 1000,
            conditional=bool(etag),
            received_etag=bool(response_headers.get("etag")),
        )
        etag = response_headers.get("etag", etag)
        if status in {429, 503}:
            try:
                retry_after = float(response_headers.get("retry-after", "0"))
            except ValueError:
                retry_after = 0.0
            delay = max(poll_seconds, retry_after)
        else:
            delay = poll_seconds
        time.sleep(max(0.1, delay * random.uniform(0.8, 1.2)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    credentials = parser.add_mutually_exclusive_group(required=True)
    credentials.add_argument("--users-file", type=Path)
    credentials.add_argument(
        "--repeat-user",
        action="store_true",
        help=(
            "prompt for one approved account and simulate every session with "
            "it without storing credentials"
        ),
    )
    parser.add_argument("--users", type=int, default=316)
    parser.add_argument("--duration-seconds", type=float, default=600)
    parser.add_argument("--ramp-seconds", type=float, default=60)
    parser.add_argument("--poll-seconds", type=float, default=15)
    args = parser.parse_args()

    if args.repeat_user:
        name = input("Approved participant name: ").strip()
        code = getpass.getpass("Six-digit trading code: ").strip()
        selected = [{"name": name, "code": code}] * args.users
    else:
        users = json.loads(args.users_file.read_text(encoding="utf-8"))
        if not isinstance(users, list) or len(users) < args.users:
            parser.error(
                "users-file does not contain the requested number of users"
            )
        selected = users[: args.users]
    if any(not user.get("name") or not user.get("code") for user in selected):
        parser.error("every user requires non-empty name and code fields")

    results = Results()
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.users) as executor:
        futures = [
            executor.submit(
                run_user,
                index,
                user,
                base_url=args.base_url.rstrip("/"),
                duration_seconds=args.duration_seconds,
                ramp_seconds=args.ramp_seconds,
                poll_seconds=args.poll_seconds,
                user_count=args.users,
                results=results,
            )
            for index, user in enumerate(selected)
        ]
        for future in futures:
            future.result()

    latencies = results.latencies_ms
    report = {
        "users": args.users,
        "login_failures": results.login_failures,
        "requests": len(latencies),
        "statuses": dict(sorted(results.statuses.items())),
        "conditional_requests": results.conditional_requests,
        "etag_responses": results.etag_responses,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "latency_ms": latency_summary(latencies),
        "latency_by_status_ms": {
            str(status): latency_summary(values)
            for status, values in sorted(results.latencies_by_status.items())
        },
    }
    print(json.dumps(report, sort_keys=True))
    steady_latencies = results.latencies_by_status.get(304, [])
    steady_p95 = percentile(
        steady_latencies if steady_latencies else latencies,
        0.95,
    )
    unhealthy = (
        results.login_failures
        or any(status >= 500 for status in results.statuses)
        or steady_p95 >= 500
        or report["latency_ms"]["p99"] >= 2000
    )
    raise SystemExit(1 if unhealthy else 0)


if __name__ == "__main__":
    main()
