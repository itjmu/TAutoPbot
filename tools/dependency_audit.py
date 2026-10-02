"""Query OSV for installed application dependency versions, without credentials."""

import importlib.metadata
import json
from pathlib import Path

import requests

try:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError:
    from pip._vendor.packaging.requirements import Requirement
    from pip._vendor.packaging.utils import canonicalize_name


def installed_dependencies():
    pending = [
        Requirement(line)
        for line in (Path(__file__).resolve().parents[1] / "requirements.txt")
        .read_text()
        .splitlines()
        if line and not line.startswith("#")
    ]
    visited = set()
    installed = {}
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        extras = frozenset(requirement.extras)
        if (name, extras) in visited:
            continue
        visited.add((name, extras))
        distribution = importlib.metadata.distribution(name)
        installed[name] = distribution.version
        for value in distribution.requires or ():
            dependency = Requirement(value)
            if dependency.marker is None or any(
                dependency.marker.evaluate({"extra": extra}) for extra in ["", *extras]
            ):
                pending.append(dependency)
    return installed


def main():
    packages = installed_dependencies()
    entries = sorted(packages.items())
    response = requests.post(
        "https://api.osv.dev/v1/querybatch",
        json={
            "queries": [
                {"package": {"name": name, "ecosystem": "PyPI"}, "version": version}
                for name, version in entries
            ]
        },
        timeout=45,
    )
    response.raise_for_status()
    results = response.json()["results"]
    if len(results) != len(entries):
        raise ValueError("Incomplete advisory response")
    findings = [
        {
            "package": name,
            "version": version,
            "advisories": [item["id"] for item in result.get("vulns", [])],
        }
        for (name, version), result in zip(entries, results)
        if result.get("vulns")
    ]
    print(
        json.dumps(
            {
                "packages_checked": len(entries),
                "versions": packages,
                "findings": findings,
                "source": "https://api.osv.dev/v1/querybatch",
            },
            indent=2,
        )
    )
    return bool(findings)


if __name__ == "__main__":
    raise SystemExit(main())
