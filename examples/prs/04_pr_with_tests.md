# Raise InvalidUrl if host starts with '.' character.

Closes #5367

Attempting to get `http://.example.com` results in a `UnicodeError` but should be raise `InvalidUrl` as attempting to get `http://*example.com`.

I've added `InvalidUrl` tests for `http://.example.com` and `http://*example.com`.
