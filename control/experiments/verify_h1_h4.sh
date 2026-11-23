#!/usr/bin/env bash
# Run the focused H1-H4 operator and component regression suites.
# Execute from a checkout with the required Python service dependencies.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

PYTHON="${PYTHON:-python3}"

printf '%s\n' '[1/4] Operator procedures H1-H4 and shared CLI contracts'
"$PYTHON" -m unittest discover -s control/tests -p 'test_h?_run.py' -v
"$PYTHON" -m unittest discover -s control/tests -p 'test_experiment_compatibility.py' -v

printf '%s\n' '[2/4] VM-NODES runtime bias control'
PYTHONPATH="nodes/services/nodes:shared${PYTHONPATH:+:$PYTHONPATH}" \
  "$PYTHON" -m unittest discover -s nodes/tests -p 'test_bias_control.py' -v

printf '%s\n' '[3/4] G2 original, mutation and replay contracts'
for pattern in test_g2.py test_edge_control.py test_h3_mutation_evidence.py test_h4_replay_evidence.py; do
  PYTHONPATH="edge/services/g2:shared${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" -m unittest discover -s edge/tests -p "$pattern" -v
done

printf '%s\n' '[4/4] VM-CORE exact telemetry and diagnostic selection'
"$PYTHON" -c "import base64,io,pathlib,zipfile; p=pathlib.Path('core/vendor/e0/e0_source.zip.b64'); z=zipfile.ZipFile(io.BytesIO(base64.b64decode(p.read_text()))); assert z.testzip() is None; z.extractall('core/vendor/e0')"

for pattern in test_experiment_api.py test_operator_api.py test_h3_core_selection.py test_h4_replay_selection.py; do
  PYTHONPATH="core/services/core:core/vendor/e0/EACLD_E0_Experimental_Package:shared${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" -m unittest discover -s core/tests -p "$pattern" -v
done
