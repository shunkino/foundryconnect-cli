import argparse
import shutil
import sys
from pathlib import Path

from . import __version__, adapters, auth, configuration, diagnostics, profiles
from .models import AGENTS, FoundryError, Profile


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="foundryconnect",
        description="Configure renewable Microsoft Foundry authentication for coding agents.",
        epilog="No API keys, token files, proxy or refresh daemon. Initial provider changes require "
               "an agent restart; routine token renewal does not.")
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    login = commands.add_parser("login", help="Check Azure CLI context; sign in only with --sign-in")
    login.add_argument("--profile")
    login.add_argument("--tenant")
    login.add_argument("--subscription")
    login.add_argument("--sign-in", action="store_true")
    login.add_argument("--device-code", action="store_true")
    profile = commands.add_parser("profile", help="Manage credential-free connection profiles")
    profile_commands = profile.add_subparsers(dest="operation", required=True)
    add = profile_commands.add_parser("add", help="Create a profile (no management-plane access needed)")
    add.add_argument("name")
    add.add_argument("--endpoint", required=True, help="Azure resource root or protocol base URL")
    add.add_argument("--deployment", required=True)
    add.add_argument("--tenant", required=True, help="Tenant UUID")
    add.add_argument("--subscription", required=True, help="Subscription UUID")
    add.add_argument("--protocol", choices=("responses", "chat", "anthropic"), required=True)
    profile_commands.add_parser("list", help="List profiles without credentials")
    for agent in AGENTS:
        command = commands.add_parser(agent, help=f"Configure {agent}")
        operations = command.add_subparsers(dest="operation", required=True)
        on = operations.add_parser("on", help="Preview and install renewable authentication")
        on.add_argument("--profile")
        on.add_argument("--dry-run", action="store_true", help="Preview only; no auth, inference or file writes")
        on.add_argument("--yes", action="store_true", help="Approve the displayed configuration changes")
        operations.add_parser("status", help="Report ownership, version and current authentication readiness")
        off = operations.add_parser("off", help="Restore owned settings; never log out or revoke tokens")
        off.add_argument("--dry-run", action="store_true")
        off.add_argument("--yes", action="store_true")
    doctor = commands.add_parser("doctor", help="Check context and token acquisition; inference is opt-in")
    doctor.add_argument("--profile")
    doctor.add_argument("--agent", choices=AGENTS, default="codex",
                        help="Select adapter protocol and token audience (default: codex)")
    doctor.add_argument("--smoke-test", action="store_true",
                        help="Explicitly authorize one small inference request; may incur charges")
    credential = commands.add_parser("auth", help="Internal command-backed credential interface")
    credentials = credential.add_subparsers(dest="operation", required=True)
    token = credentials.add_parser("token", help="Print ONLY a raw bearer token (sensitive; do not log)")
    token.add_argument("--profile", required=True)
    token.add_argument("--home", type=Path, help=argparse.SUPPRESS)
    return root


def approve(yes: bool, action: str) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise FoundryError(f"{action} requires approval. Review --dry-run, then repeat with --yes.")
    try:
        answer = input(f"{action}? [y/N] ")
    except EOFError as exc:
        raise FoundryError("No approval received; no configuration changed.") from exc
    if answer.strip().lower() not in ("y", "yes"):
        raise FoundryError("Cancelled; no configuration changed.")


def detect() -> None:
    print("Azure CLI: " + ("installed" if shutil.which("az") else "not installed"))
    print("Agents detected: " + (", ".join(agent for agent in AGENTS if shutil.which(agent)) or "none"))


def on(args, root: Path) -> None:
    profile = profiles.get(root, args.profile)
    plan = adapters.make_plan(args.command, profile)
    prepared = configuration.prepare(plan)
    adapters.check_conflicts(plan, configuration.parse(prepared.before or "", plan.format))
    print(configuration.preview(prepared))
    if args.dry_run:
        print("Preview only. Version, credentials and inference have not been verified.")
        for line in plan.guidance:
            print(line)
        return
    version = adapters.check_version(args.command)
    print(f"Agent version: {version}")
    auth.account(profile)
    auth.token(profile, adapters.scope_for(args.command, profile))
    print("Credentials currently obtainable: yes (Azure CLI). Inference: not verified.")
    approve(args.yes, "Install this configuration")
    changed = configuration.install(root, prepared, profile.name)
    print("Configuration installed." if changed else "Configuration already installed; no changes.")
    native_ready, message = adapters.native_login_status(args.command)
    print(message)
    print("Initial restart required to activate provider changes. Once native setup is complete, "
          "routine token renewal does not require restarting.")
    if not native_ready:
        print("Setup incomplete: finish native login before launching a coding session.")
    for line in plan.guidance:
        print(line)


def status(agent: str, root: Path) -> int:
    data = configuration.receipt(root, agent)
    if not data:
        print(f"{agent}: not managed by FoundryConnect. Existing agent settings were not changed.")
        return 0
    print(f"{agent}: managed profile {data['profile']}; configuration {data['path']}")
    changed = configuration.drift(data)
    if changed:
        raise FoundryError("Configuration drift: " + ", ".join(changed) + ". No settings changed.")
    profile = profiles.get(root, data["profile"])
    plan = adapters.make_plan(agent, profile)
    if str(plan.path) != data["path"]:
        raise FoundryError("The current agent config location differs from the installed location. "
                           "Restore the previous environment or run off (which uses the recorded path).")
    _, document = configuration.read(plan.path, plan.format)
    adapters.check_conflicts(plan, document)
    print(f"Configuration installed: yes. Authentication: {plan.strategy}")
    print(f"Agent version: {adapters.check_version(agent)}")
    auth.account(profile)
    auth.token(profile, adapters.scope_for(agent, profile))
    print("Credentials currently obtainable: yes (Azure CLI).")
    ready, message = adapters.native_login_status(agent)
    print(message)
    print("Inference: not verified by status. Runtime overrides and a changed Azure CLI context "
          "can affect an agent session.")
    for line in plan.guidance:
        print(line)
    return 0 if ready else 1


def run(args) -> int:
    if sys.platform == "win32":
        raise FoundryError("Native Windows is not supported; use WSL2.", "platform")
    root = profiles.home()
    if args.command == "profile":
        if args.operation == "add":
            profile = profiles.add(root, Profile(args.name, args.endpoint, args.deployment,
                                                args.tenant, args.subscription, args.protocol))
            print(f"Created profile {profile.name}: {profile.endpoint} ({profile.protocol}), "
                  f"deployment {profile.deployment}. No credentials stored.")
        else:
            values = profiles.load_all(root)
            if not values:
                print("No profiles. Use foundryconnect profile add NAME --help.")
            for profile in values.values():
                print(f"{profile.name}\t{profile.protocol}\t{profile.endpoint}\t{profile.deployment}"
                      f"\ttenant={profile.tenant}\tsubscription={profile.subscription}")
    elif args.command == "login":
        detect()
        profile = profiles.get(root, args.profile) if args.profile else None
        if profile and ((args.tenant and args.tenant.lower() != profile.tenant)
                        or (args.subscription and args.subscription.lower() != profile.subscription)):
            raise FoundryError("--tenant/--subscription conflict with --profile.")
        data = auth.login(profile, args.sign_in, args.tenant, args.subscription, args.device_code)
        if not profile and ((args.tenant and data["tenantId"].lower() != args.tenant.lower())
                            or (args.subscription and data["id"].lower() != args.subscription.lower())):
            raise FoundryError("The active Azure CLI context does not match --tenant/--subscription. "
                               "Use --sign-in to explicitly select it.", "authentication")
        print(f"Azure CLI authenticated: tenant {data['tenantId']}, subscription {data['id']}.")
    elif args.command == "auth":
        if args.home:
            root = args.home.expanduser().absolute()
        profile = profiles.get(root, args.profile)
        # Each invocation asks Azure CLI; FoundryConnect never caches credentials.
        credential = auth.token(profile, adapters.scope_for("codex", profile))
        print(credential.value)
    elif args.command == "doctor":
        profile = profiles.get(root, args.profile)
        detect()
        plan = adapters.make_plan(args.agent, profile)
        print(f"Endpoint shape: valid ({profile.protocol}); deployment capability: not yet verified.")
        print(f"Agent version: {adapters.check_version(args.agent)}")
        _, document = configuration.read(plan.path, plan.format)
        adapters.check_conflicts(plan, document)
        auth.account(profile)
        scope = adapters.scope_for(args.agent, profile)
        credential = auth.token(profile, scope)
        print(f"Authentication: token obtainable for {scope}. Tenant/subscription match.")
        ready, message = adapters.native_login_status(args.agent)
        print(message)
        for line in plan.guidance:
            print(line)
        if args.smoke_test:
            diagnostics.smoke_test(profile, credential.value)
            print(f"Inference successfully verified: {profile.protocol}. "
                  "This verifies the direct endpoint, not an already-running agent session.")
        else:
            print("Authorization, endpoint reachability and model protocol support: not verified. "
                  "Use --smoke-test to approve a small, potentially billable inference request.")
        return 0 if ready else 1
    elif args.command in AGENTS:
        if args.operation == "on":
            on(args, root)
        elif args.operation == "status":
            return status(args.command, root)
        else:
            data = configuration.receipt(root, args.command)
            if data is None:
                print("No owned configuration to remove. Azure CLI login and credentials unchanged.")
                return 0
            configuration.uninstall(root, args.command, dry_run=True)
            print(f"Restore owned settings in {data['path']}; preserve unrelated changes.")
            if args.dry_run:
                print("Preview only; no configuration changed.")
                return 0
            approve(args.yes, "Remove FoundryConnect configuration")
            configuration.uninstall(root, args.command)
            print("Owned configuration restored/removed. Restart the agent to activate the change. "
                  "Azure CLI was not logged out; issued tokens were not revoked.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return run(args)
    except FoundryError as exc:
        print(f"{exc.category}: {exc}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        # OS errors can include file contents or sensitive paths supplied by external commands.
        print("io: Could not read/write a local file or run a command. Check permissions, "
              "encoding and available disk space.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
