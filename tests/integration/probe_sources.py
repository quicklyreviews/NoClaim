"""Which price sources can GenLayer's validators actually reach?

Not a test -- a diagnostic. Binance answered the first consensus run with
"service unavailable from a restricted location", so the validator nodes are
geo-blocked from it. Guessing the next URL costs 6 minutes a try through a real
market, so this deploys a throwaway contract that just fetches and reports.

    gltest tests/integration/probe_sources.py --network studionet -v -s
"""

import time

from gltest import get_contract_factory


# Everything the frontend offers as a starter template has to be checked here
# first. Shipping a template pointed at a host validators cannot reach would
# hand every user the Binance failure as their first experience of the product.
SOURCES = {
    # world events and physical reality
    "usgs quakes": "https://earthquake.usgs.gov/fdsnws/event/1/count?format=geojson&minmagnitude=5&starttime=now-1days",
    "opensky flights": "https://opensky-network.org/api/states/all?lamin=45&lomin=5&lamax=55&lomax=20",
    "people in space": "http://api.open-notify.org/astros.json",
    "iss position": "http://api.open-notify.org/iss-now.json",
    # global markets
    "coingecko global": "https://api.coingecko.com/api/v3/global",
    "frankfurter fx": "https://api.frankfurter.app/latest?base=USD&symbols=VND",
    # global culture
    "wikipedia pageviews": "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/all-agents/Bitcoin/daily/20260901/20260903",
    # air quality (different open-meteo subdomain, so probed separately)
    "open-meteo air": "https://air-quality-api.open-meteo.com/v1/air-quality?latitude=21.0278&longitude=105.8342&hourly=pm2_5&forecast_days=1",
}


def test_probe_which_sources_validators_can_reach(tmp_path):
    contract_src = '''# v0.0.1 -- source reachability probe
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

from genlayer import *

import typing


class Probe(gl.Contract):
    last: str

    def __init__(self):
        self.last = ""

    @gl.public.write
    def fetch(self, url: str) -> str:
        """Leader-only fetch, reported verbatim. No equivalence check: the point
        is to see what a validator node actually receives, including an error
        body, not to agree on it."""

        def leader_fn() -> str:
            try:
                return str(gl.nondet.web.get(url).body)[:400]
            except Exception as e:
                return f"EXCEPTION: {type(e).__name__}: {str(e)[:200]}"

        def validator_fn(leaders_res) -> bool:
            return True

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        self.last = str(result)[:400]
        return self.last

    @gl.public.view
    def get_last(self) -> str:
        return self.last
'''
    path = tmp_path / "probe_sources_contract.py"
    path.write_text(contract_src, encoding="ascii")

    factory = get_contract_factory(contract_file_path=str(path))
    contract = factory.deploy(args=[])
    print(f"\nProbe deployed at {contract.address}\n")

    for name, url in SOURCES.items():
        try:
            contract.fetch(args=[url]).transact()
            body = contract.get_last(args=[]).call()
            verdict = "OK" if not body.startswith("EXCEPTION") else "FAIL"
            print(f"--- {name}: {verdict}\n    {body[:220]}\n")
        except Exception as e:  # noqa: BLE001
            print(f"--- {name}: TX FAILED\n    {type(e).__name__}: {str(e)[:200]}\n")
        time.sleep(1)
