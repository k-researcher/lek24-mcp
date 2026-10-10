"""Optional external matching helper; its output never changes confirmed coverage."""

from __future__ import annotations

import asyncio
import json

from pydantic import ValidationError

from lek24_mcp.coverage import LicenseSite, MatchingHint


async def add_hints(
    sites: list[LicenseSite], command: list[str], *, timeout: float = 30, max_pairs: int = 50
) -> tuple[list[LicenseSite], list[str]]:
    pairs: list[dict[str, object]] = []
    targets: dict[str, tuple[int, int]] = {}
    for index, site in enumerate(sites):
        if not (len(site.inn) == 10 and site.inn.isdigit()):
            continue
        if site.status != "uncertain":
            continue
        for pharmacy in site.candidates:
            if len(pairs) >= max_pairs:
                break
            key = str(len(pairs))
            targets[key] = (index, pharmacy.id)
            pairs.append(
                {
                    "id": key,
                    "license": {"names": site.names, "address": site.address},
                    "pharmacy": {"name": pharmacy.name, "address": pharmacy.address},
                }
            )
    if not pairs:
        return sites, []
    limited = (
        sum(
            len(site.candidates)
            for site in sites
            if site.status == "uncertain" and len(site.inn) == 10 and site.inn.isdigit()
        )
        > max_pairs
    )
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            process.communicate(json.dumps({"pairs": pairs}, ensure_ascii=False).encode()), timeout=timeout
        )
        if process.returncode != 0 or len(stdout) > 100_000:
            raise ValueError("helper failed")
        payload = json.loads(stdout)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"hints"}
            or not isinstance(payload["hints"], dict)
        ):
            raise ValueError("invalid helper response")
        hints: dict[int, list[MatchingHint]] = {}
        if set(payload["hints"]) != set(targets):
            raise ValueError("incomplete helper response")
        for key, value in payload["hints"].items():
            index, pharmacy_id = targets[key]
            hint = MatchingHint.model_validate({**value, "pharmacy_id": pharmacy_id})
            hints.setdefault(index, []).append(hint)
        updated = [site.model_copy(update={"hints": hints.get(i, [])}) for i, site in enumerate(sites)]
        warnings = ["External matching hints are advisory; uncertain matches remain uncertain."]
        if limited:
            warnings.append(f"Optional helper limited to {max_pairs} pairs; remaining pairs not evaluated.")
        return updated, warnings
    except (TimeoutError, OSError, ValueError, TypeError, ValidationError):
        return sites, ["Optional matching helper unavailable or invalid; ordinary matching completed."]
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
