#!/usr/bin/python -I
"""Generate a hash-pinnable deployment manifest without privileges or host writes."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "application"))
from nitro_control.deployment import DeploymentError, create_plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, help="new workspace JSON file; default: JSON to stdout")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() == 0:
            raise DeploymentError("Run the generator as an ordinary user, never through pkexec/sudo.")
        plan = create_plan(args.workspace)
        data = (json.dumps(plan, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
        if args.output:
            workspace = args.workspace.resolve(strict=True)
            output = args.output.absolute()
            if not output.parent.resolve(strict=True).is_relative_to(workspace) or output.is_symlink():
                raise DeploymentError("Output must be a new file inside the workspace.")
            with output.open("xb") as stream:
                stream.write(data)
            print(json.dumps({"manifest": str(output), "sha256": hashlib.sha256(data).hexdigest()}))
        else:
            sys.stdout.write(data.decode("ascii"))
        return 0
    except (DeploymentError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print("Deployment plan blocked: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
