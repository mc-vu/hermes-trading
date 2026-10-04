"""Binance Spot-Kontostatus und Bestaende (GET-only, HMAC signiert).

Nur die zwei Pfade in ALLOWED_PATHS werden signiert abgefragt. Jeder andere Pfad
wird vor dem Request mit BinanceReadOnlyError abgewiesen.
"""
from __future__ import annotations
import hashlib, hmac, os, time
from urllib.parse import urlencode
from .adapters.http import HttpClient

BASE = "https://api.binance.com"
RIGHTS_PATH = "/sapi/v1/account/apiRestrictions"
ACCOUNT_PATH = "/api/v3/account"
ALLOWED_PATHS = frozenset({RIGHTS_PATH, ACCOUNT_PATH})
# Jedes dieser Rechte macht den Schluessel unbrauchbar: Handel, Auszahlung oder Umbuchung.
FORBIDDEN_RIGHTS = ("enableSpotAndMarginTrading", "enableWithdrawals", "enableMargin", "enableFutures",
                    "enableVanillaOptions", "enableInternalTransfer", "permitsUniversalTransfer",
                    "enablePortfolioMarginTrading", "enableFixApiTrade")
TOO_MANY_RIGHTS = "Schlüssel hat zu viele Rechte"


class BinanceReadOnlyError(RuntimeError): pass


class BinanceAccount:
    def __init__(self, client=None, api_key=None, api_secret=None, clock=None):
        self.client=client or HttpClient()
        self.api_key=api_key if api_key is not None else os.environ.get("BINANCE_API_KEY","")
        self.api_secret=api_secret if api_secret is not None else os.environ.get("BINANCE_API_SECRET","")
        self.clock=clock or (lambda:int(time.time()*1000))

    def _get_signed(self,path):
        if path not in ALLOWED_PATHS:
            raise BinanceReadOnlyError(f"Pfad nicht erlaubt: {path}")
        if not self.api_key or not self.api_secret: raise BinanceReadOnlyError("Binance-Schlüssel fehlen (BINANCE_API_KEY/BINANCE_API_SECRET)")
        query=urlencode({"timestamp":self.clock(),"recvWindow":5000})
        sig=hmac.new(self.api_secret.encode(),query.encode(),hashlib.sha256).hexdigest()
        return self.client.get_json(BASE+path+"?"+query+"&signature="+sig,headers={"X-MBX-APIKEY":self.api_key})

    def check_rights(self):
        rights=self._get_signed(RIGHTS_PATH)
        if not isinstance(rights, dict) or any(rights.get(k) for k in FORBIDDEN_RIGHTS):
            raise BinanceReadOnlyError(TOO_MANY_RIGHTS)
        return rights

    def read_balances(self):
        self.check_rights()
        account=self._get_signed(ACCOUNT_PATH)
        if account.get("canTrade") or account.get("canWithdraw"):
            raise BinanceReadOnlyError(TOO_MANY_RIGHTS)
        return [{"asset":b["asset"],"free":float(b["free"]),"locked":float(b["locked"])} for b in account.get("balances",[]) if float(b.get("free",0)) or float(b.get("locked",0))]
