import json
from pathlib import Path

import pytest

from iicp_client.cli import main
from iicp_client.completion import candidates, script


FIXTURE = json.loads((Path(__file__).parents[1] / "parity/cli-completion-v1.json").read_text())


@pytest.mark.parametrize("case", FIXTURE["cases"])
def test_completion_parity(case):
    result = candidates(case["tokens"])
    assert set(case["contains"]) <= set(result)


@pytest.mark.parametrize("shell", FIXTURE["shells"] + ["pwsh"])
def test_scripts_are_static(shell):
    rendered = script(shell)
    assert "iicp-node __complete" in rendered
    assert "IICP_DIRECTORY" not in rendered


def test_hidden_completion_stdout(capsys):
    assert main(["__complete", "op"]) == 0
    assert capsys.readouterr().out == "operator\n"


def test_public_completion(capsys):
    assert main(["completion", "bash"]) == 0
    assert "complete -F" in capsys.readouterr().out
