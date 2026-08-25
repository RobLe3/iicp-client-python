"""Side-effect-free shell completion for the ``iicp-node`` CLI."""

from __future__ import annotations

COMMANDS = ("completion", "credits", "doctor", "healthcheck", "help", "init", "list", "mcp-gateway", "operator", "proxy", "query", "serve", "service", "update")
SUBCOMMANDS = {
    ("operator",): ("decrypt", "dsr", "encrypt", "key", "rename"),
    ("operator", "dsr"): ("anonymize", "export", "restrict"),
    ("operator", "key"): ("export", "generate", "import", "list", "revoke", "rotate"),
    ("service",): ("install", "restart", "status", "uninstall"),
}
OPTIONS = {
    (): ("--help", "--version"),
    ("query",): ("--directory", "--intent", "--json", "--node", "--routing-profile"),
    ("serve",): ("--backend-type", "--directory", "--host", "--node", "--port", "--routing-profile"),
}
VALUES = {
    ("query", "--routing-profile"): ("eu-restricted", "sensitive", "standard", "strict-policy"),
    ("serve", "--backend-type"): ("anthropic", "llamacpp", "meshllm", "openai_compat", "vllm"),
}


def candidates(tokens: list[str]) -> list[str]:
    """Return static candidates without reading operator or network state."""
    if not tokens:
        return list(COMMANDS)
    partial = tokens[-1]
    prior = tokens[:-1]
    command = prior[0] if prior else ""
    for (owner, option), values in VALUES.items():
        if command == owner and prior and prior[-1] == option:
            return [value for value in values if value.startswith(partial)]
    path = tuple(item for item in prior if not item.startswith("-") and item not in ("",))
    choices = list(SUBCOMMANDS.get(path, ()))
    if not prior:
        choices.extend(COMMANDS)
    context = (command,) if command else ()
    if partial.startswith("-"):
        choices = list(OPTIONS.get(context, ())) + list(OPTIONS[()])
    return sorted({choice for choice in choices if choice.startswith(partial)})


def script(shell: str) -> str:
    shell = "powershell" if shell == "pwsh" else shell
    scripts = {
        "bash": '''_iicp_node_complete() {\n  COMPREPLY=()\n  local -a args=("${COMP_WORDS[@]:1:$COMP_CWORD}")\n  while IFS= read -r candidate; do COMPREPLY+=("$candidate"); done < <(command iicp-node __complete "${args[@]}")\n}\ncomplete -F _iicp_node_complete iicp-node\n''',
        "zsh": '''_iicp_node_complete() {\n  local -a args candidates\n  args=("${words[@]:1}")\n  candidates=("${(@f)$(command iicp-node __complete "${args[@]}")}")\n  compadd -- $candidates\n}\ncompdef _iicp_node_complete iicp-node\n''',
        "fish": '''function __iicp_node_complete\n  set -l tokens (commandline -opc)\n  set -e tokens[1]\n  set -a tokens (commandline -ct)\n  command iicp-node __complete $tokens\nend\ncomplete -c iicp-node -f -a '(__iicp_node_complete)'\n''',
        "powershell": '''Register-ArgumentCompleter -Native -CommandName iicp-node -ScriptBlock {\n  param($wordToComplete, $commandAst, $cursorPosition)\n  $tokens = @($commandAst.CommandElements | Select-Object -Skip 1 | ForEach-Object { $_.Extent.Text })\n  if ($tokens.Count -eq 0 -or $commandAst.Extent.Text.EndsWith(' ')) { $tokens += '' }\n  iicp-node __complete @tokens | Where-Object { $_ -like "$wordToComplete*" } | ForEach-Object { [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_) }\n}\n''',
    }
    if shell not in scripts:
        raise ValueError(f"unsupported shell: {shell}")
    return scripts[shell]
