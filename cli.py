"""CLI da Aurora: status, pergunta única e REPL por terminal."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from aurora import __version__
from aurora.config.permissions import Risk
from aurora.config.settings import ConfigError, Settings
from aurora.core.planner import PlannerError
from aurora.llm.provider import ProviderError
from aurora.observability import setup_logging
from aurora.runtime import Runtime, build_runtime
from aurora.voice.tts_engine import create_tts, describe_voices, list_sapi_voices

HELP = """
Comandos:
  /ajuda                     esta lista
  /status                    diagnóstico do ambiente e do provedor
  /ferramentas               ferramentas disponíveis e nível de risco
  /voz on|off|status         liga, desliga ou mostra a voz
  /diga <texto>              fala um texto pelo TTS (teste de japonês)
  /memorias [n]              lista memórias gravadas
  /esquecer <id>             apaga uma memória específica
  /exportar-memorias [caminho]  exporta memórias para JSON no workspace
  /plano <objetivo>          planeja com ferramentas, mostra e executa com aprovação
  /tarefas                   tarefas recentes e pendentes
  /retomar <task_id>         retoma uma tarefa a partir do checkpoint
  /novo                      inicia nova sessão de conversa
  /cancelar                  solicita cancelamento do turno atual
  /sair                      encerra

Qualquer outra linha é enviada ao agente. Ctrl+C interrompe o turno.
""".strip()


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _ok(flag: bool) -> str:
    return "[ok]" if flag else "[--]"


def print_status(settings: Settings, runtime: Runtime | None = None) -> int:
    print(f"\nAurora {__version__} — diagnóstico")
    print("-" * 58)
    print(f"Python       {sys.version.split()[0]} ({sys.executable})")
    print(f"Workspace    {settings.paths.workspace_dir}")
    print(f"Raízes       {', '.join(str(root) for root in settings.paths.allowed_roots)}")
    print(f"Banco        {settings.paths.db_path}")

    critical = True
    try:
        settings.ensure_directories()
        from aurora.storage.database import Database
        from aurora.memory.long_term import LongTermMemory

        db = Database(settings.paths.db_path)
        applied = db.migrate()
        memory = LongTermMemory(db)
        print(f"Migrações    {', '.join(applied) if applied else 'já atualizadas'}")
        print(f"Memórias     {memory.count()}")
        db.close()
        print(f"Logs         {settings.paths.logs_dir / 'aurora.log'}")
    except Exception as exc:
        critical = False
        print(f"Banco        ERRO: {exc}")

    print(f"Chave LLM    {'configurada' if settings.has_api_key() else 'AUSENTE'}")
    print(f"Gateway      {settings.llm.base_url}  modelo padrão: {settings.llm.model}")

    if settings.has_api_key():
        try:
            from aurora.llm.freellm_client import FreeLLMClient

            client = FreeLLMClient(
                settings.llm.base_url,
                settings.api_key(),
                timeout_s=min(settings.llm.timeout_s, 15),
                max_retries=0,
            )
            health = client.health()
            client.close()
            if health["ok"]:
                print(f"FreeLLM      {_ok(True)} online — {health['models']} modelos, {health['latency_ms']} ms")
            else:
                print(f"FreeLLM      {_ok(False)} {health['kind']}: {health['error']}")
        except ConfigError as exc:
            print(f"FreeLLM      {_ok(False)} {exc}")
        except Exception as exc:
            print(f"FreeLLM      {_ok(False)} {type(exc).__name__}: {exc}")
    else:
        print("FreeLLM      não verificado (sem chave)")

    registry = runtime.registry if runtime else None
    if registry is None:
        from aurora.tools import build_registry

        registry = build_registry()
    enabled = settings.enabled_tools or tuple(registry.names())
    print(f"Ferramentas  {len(enabled)} habilitadas: {', '.join(registry.names())}")
    print(f"Aprovação    automática até {settings.auto_approve_max_risk}; acima disso pergunta")

    voices = list_sapi_voices() if sys.platform == "win32" else []
    ja = [voice for voice in voices if voice.is_japanese]
    if voices:
        print(f"Vozes SAPI   {_ok(bool(ja))} {describe_voices()}")
    else:
        print(f"Vozes SAPI   {_ok(False)} nenhuma detectada")
    print(f"VOICEVOX     {settings.voice.voicevox_url} (backend={settings.voice.backend})")
    if runtime:
        summary = runtime.metrics.summary()
        print(
            f"Métricas     {summary['chamadas_llm']} chamadas, "
            f"{summary['tokens_estimados']} tokens, p95 {summary['latencia_p95_ms']} ms"
        )
    print("-" * 58)
    return 0 if critical else 1


def _print_reply(reply) -> None:
    print(f"\naurora › {reply.text}")
    details = []
    if reply.model:
        details.append(reply.model)
    details.append(f"{reply.latency_ms} ms")
    if reply.tool_calls:
        details.append(f"{reply.tool_calls} ferramenta(s): {', '.join(reply.tools_used)}")
    if reply.status != "COMPLETED":
        details.append(f"estado={reply.status}")
    print(f"  ({' • '.join(details)})")


def _print_plan(plan, registry) -> None:
    if not plan.steps:
        print(f"Plano vazio. Observação: {plan.notes or 'nada a executar'}")
        return
    print("\nPlano proposto:")
    for step in plan.steps:
        risk = registry.get(step.tool).risk
        reason = f" — {step.why}" if step.why else ""
        print(f"  {step.index}. [{risk.label}] {step.tool} {step.args}{reason}")
    if plan.notes:
        print(f"  Observação: {plan.notes}")


def _print_task(task) -> None:
    print(f"\nTarefa {task.id}: {task.status.value}")
    for step in task.steps:
        mark = "ok" if step.ok else "falhou" if step.ok is False else step.status
        print(f"  {step.index}. [{mark}] {step.tool}: {step.summary or step.description}")
    if task.error:
        print(f"  erro: {task.error}")


def _cmd_plan(runtime: Runtime, goal: str) -> None:
    if runtime.planner is None or runtime.orchestrator is None:
        print("Planejamento indisponível sem provedor de IA configurado.")
        return
    if not goal.strip():
        print("Uso: /plano <objetivo>")
        return
    try:
        plan = runtime.planner.create_plan(goal, runtime.settings.enabled_tools)
    except ProviderError as exc:
        print(f"Falha ao planejar: {exc.user_message()} — {exc}")
        return
    except PlannerError as exc:
        print(f"Plano inválido: {exc}")
        return
    _print_plan(plan, runtime.registry)
    if not plan.steps:
        return
    try:
        answer = input("Executar este plano? [s/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in {"s", "sim", "y", "yes"}:
        print("Plano descartado.")
        return
    session_id = runtime.agent.ensure_session() if runtime.agent else ""
    task = runtime.orchestrator.run(goal, plan, session_id=session_id)
    _print_task(task)


def _cmd_memories(runtime: Runtime, rest: str) -> None:
    limit = 20
    if rest.strip().isdigit():
        limit = max(1, min(200, int(rest.strip())))
    records = runtime.memory.all(limit=limit)
    if not records:
        print("Nenhuma memória gravada.")
        return
    for record in records:
        print(f"  #{record.id} [{record.layer}/{record.kind}] {record.content}")

def _cmd_export(runtime: Runtime, rest: str) -> None:
    raw = rest.strip() or "memorias.json"
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = runtime.settings.paths.workspace_dir / candidate
    try:
        target = runtime.policy.resolve(candidate, for_write=True)
    except Exception as exc:
        print(f"Destino inválido: {exc}")
        return
    count = runtime.memory.export(target)
    print(f"{count} memória(s) exportada(s) para {target}")

def repl(runtime: Runtime) -> int:
    agent = runtime.agent
    speaker = runtime.speech
    voice_on = bool(speaker and speaker.enabled)
    print(f"\nAurora {__version__} — pronta." + ("" if agent else " (sem provedor de IA: só comandos)"))
    if not agent:
        print("Defina FREELLMAPI_API_KEY no ambiente ou no .env e reinicie para conversar.")
    print("Digite /ajuda para os comandos, /sair para encerrar.")
    if speaker and speaker.enabled:
        print(f"Voz: {speaker.backend} ({runtime.settings.voice.language})")

    while True:
        try:
            line = input("\nvocê › ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAté mais.")
            break
        if not line:
            continue

        if line.startswith("/"):
            command, _, rest = line.partition(" ")
            command = command.lower()
            if command in {"/sair", "/exit", "/quit"}:
                print("Até mais.")
                break
            if command == "/ajuda":
                print(HELP)
            elif command == "/status":
                print_status(runtime.settings, runtime)
            elif command == "/ferramentas":
                print(runtime.registry.catalog_text(runtime.settings.enabled_tools))
            elif command == "/voz":
                action = rest.strip().lower() or "status"
                if action == "on":
                    voice_on = True
                    print("Voz ligada." if speaker and speaker.enabled else "Nenhuma voz disponível.")
                elif action == "off":
                    voice_on = False
                    if speaker:
                        speaker.stop()
                    print("Voz desligada; seguindo em texto.")
                else:
                    state = "ligada" if voice_on else "desligada"
                    print(f"Voz {state}; backend={speaker.backend if speaker else 'none'}; {describe_voices()}")
            elif command == "/diga":
                text = rest.strip()
                if not text:
                    print("Uso: /diga <texto>")
                elif speaker and speaker.enabled:
                    speaker.say(text)
                    print("Fala enfileirada.")
                else:
                    print("Nenhuma voz japonesa disponível.")
            elif command == "/memorias":
                _cmd_memories(runtime, rest)
            elif command == "/esquecer":
                if not rest.strip().isdigit():
                    print("Uso: /esquecer <id>")
                elif runtime.memory.delete(int(rest.strip())):
                    print("Memória apagada.")
                else:
                    print("Memória não encontrada.")
            elif command == "/exportar-memorias":
                _cmd_export(runtime, rest)
            elif command == "/plano":
                _cmd_plan(runtime, rest)
            elif command == "/tarefas":
                tasks = runtime.tasks.recent(limit=10)
                if not tasks:
                    print("Nenhuma tarefa registrada.")
                for task in tasks:
                    print(f"  {task.id} {task.status.value} — {task.goal[:80]}")
                pending = runtime.tasks.pending()
                if pending:
                    print(f"  pendentes: {', '.join(task.id for task in pending)}")
            elif command == "/retomar":
                if runtime.orchestrator is None:
                    print("Orquestrador indisponível.")
                elif not rest.strip():
                    print("Uso: /retomar <task_id>")
                else:
                    try:
                        task = runtime.orchestrator.resume(rest.strip())
                        _print_task(task)
                    except Exception as exc:
                        print(f"Falha ao retomar: {exc}")
            elif command == "/novo":
                if agent:
                    session = agent.new_session()
                    print(f"Nova sessão: {session}")
                else:
                    print("Sem agente configurado.")
            elif command == "/cancelar":
                if agent:
                    agent.cancel()
                    print("Cancelamento solicitado.")
                if speaker:
                    speaker.stop()
            else:
                print("Comando desconhecido. Veja /ajuda.")
            continue

        if agent is None:
            print("Sem provedor configurado. Defina a chave e reinicie.")
            continue

        try:
            agent.reset_cancel()
            reply = agent.handle(line)
        except KeyboardInterrupt:
            agent.cancel()
            print("\nTurno interrompido a pedido do usuário.")
            continue
        _print_reply(reply)
        if speaker and voice_on:
            speaker.say(reply.text)

    if speaker:
        speaker.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="aurora",
        description="Aurora HUD — núcleo do assistente pessoal (Fase 1).",
    )
    parser.add_argument("--ask", help="faz uma pergunta única e encerra")
    parser.add_argument("--status", action="store_true", help="diagnóstico do ambiente")
    parser.add_argument("--speak", help="testa o TTS com o texto informado")
    parser.add_argument("--no-voice", action="store_true", help="não inicializa voz")
    parser.add_argument("--version", action="version", version=f"aurora {__version__}")
    args = parser.parse_args(argv)

    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"Configuração inválida: {exc}")
        return 2

    setup_logging(settings.paths.logs_dir, settings.log_level, console=True)

    if args.status:
        return print_status(settings)

    if args.speak is not None:
        engine = create_tts(settings.voice, work_dir=settings.paths.data_dir / "voice")
        print(f"Vozes: {describe_voices()}")
        print(f"Backend: {engine.name}")
        try:
            engine.speak(args.speak)
            print("Síntese concluída.")
        except Exception as exc:
            print(f"Falha na síntese: {exc}")
            return 1
        finally:
            engine.close()
        return 0

    try:
        runtime = build_runtime(settings, voice=not args.no_voice)
    except Exception as exc:
        print(f"Falha ao iniciar o núcleo: {type(exc).__name__}: {exc}")
        return 2

    try:
        if args.ask:
            if runtime.agent is None:
                print("Sem provedor de IA configurado (chave ausente).")
                return 2
            reply = runtime.agent.handle(args.ask)
            _print_reply(reply)
            return 0 if reply.status == "COMPLETED" else 1
        return repl(runtime)
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
