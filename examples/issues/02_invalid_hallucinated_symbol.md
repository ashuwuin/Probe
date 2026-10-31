# Security: parse_url_v2() leaks proxy credentials

`parse_url_v2()` in `src/requests/utils.py` does not strip the userinfo part of proxy URLs before logging them, so `user:password` ends up in debug logs.

This affects every request that goes through a proxy. Please patch `parse_url_v2()` to call `urldefragauth` first.
