# LOG-01 oracle

The confirmation builder handles delivery and pickup orders. A pickup order has no
shipping address and returns a pickup destination in the pre-image. The patch
adds a shipping-zone field to every confirmation, including pickup, and reads
`zone` from the absent address. Valid pickup orders now raise `AttributeError`
instead of returning a confirmation. Delivery orders still return the new zone,
which makes the defect specific to the optional-address path.

Ground truth is one `correctness` / `high` finding on the added
`shipping_zone` expression at line 31, yielding `blocking`. It affects every
pickup order through this function, but the fixture does not establish a
service-wide outage that would justify `critical`. The source is synthetic,
and neither pre-image nor patch contains answer-hint comments.

This dependency-free oracle checks both public input paths before and after
applying the patch to a temporary copy of the pre-image:

```sh
uv run python - <<'PY'
import runpy
import shutil
import subprocess
import tempfile
from pathlib import Path

case = Path("test-prs-dataset/cases/LOG-01").resolve()


def observe(source):
    module = runpy.run_path(str(source))
    order = module["Order"]
    address = module["ShippingAddress"]
    pickup = order(1042, "pickup", None)
    delivery = order(1043, "delivery", address("Krakow", "pl-south"))
    try:
        pickup_result = module["build_confirmation"](pickup)
    except AttributeError:
        pickup_result = "AttributeError"
    return pickup_result, module["build_confirmation"](delivery)


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    shutil.copytree(case / "base", root, dirs_exist_ok=True)
    source = root / "orders/confirmation.py"
    before = observe(source)
    assert before == (
        {"order_id": "1042", "destination": "Store pickup"},
        {"order_id": "1043", "destination": "Krakow"},
    )
    subprocess.run(["git", "apply", str(case / "diff.patch")], cwd=root, check=True)
    after = observe(source)
    assert after == (
        "AttributeError",
        {
            "order_id": "1043",
            "destination": "Krakow",
            "shipping_zone": "PL-SOUTH",
        },
    )
    print("pickup confirmation succeeds before patch and fails after")
PY
```
