"""Painel auxiliar da HUD: histórico local, diagnóstico e preferências."""
from __future__ import annotations

import socket
import threading
from urllib.parse import urlsplit
from typing import Any, Callable, Mapping

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from aurora.ui.hud_preferences import normalize_hud_preferences


class AssistantPanelDialog(QDialog):
    """Painel de ferramentas intencionalmente separado da HUD compacta."""

    diagnostics_ready = Signal(object)

    def __init__(
        self,
        parent: QWidget,
        *,
        runtime: Any,
        preferences: Mapping[str, Any],
        preferences_path,
        on_preferences_saved: Callable[[dict[str, Any]], Any],
        diagnostics_provider: Callable[[], dict[str, Any]],
        is_busy: Callable[[], bool],
        on_session_deleted: Callable[[str | None], Any],
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle("Aurora — painel")
        self.resize(820, 560)
        self.setMinimumSize(680, 440)
        self.setStyleSheet(
            "QDialog { background: #07131d; color: #d9faff; }"
            "QLabel { color: #9dffff; }"
            "QTextBrowser, QListWidget { background: #091c29; color: #d9faff; "
            "border: 1px solid #176078; border-radius: 6px; padding: 8px; }"
            "QPushButton { color: #9dffff; background: #103447; "
            "border: 1px solid #1685a5; border-radius: 5px; padding: 6px 10px; }"
            "QPushButton:hover { background: #15516a; }"
            "QPushButton:disabled { color: #607f8a; border-color: #29434c; }"
            "QCheckBox { spacing: 7px; }"
            "QSpinBox { background: #091c29; color: #d9faff; border: 1px solid #176078; padding: 4px; }"
            "QTabWidget::pane { border: 1px solid #176078; }"
            "QTabBar::tab { background: #0b2635; color: #9dffff; padding: 8px 14px; }"
            "QTabBar::tab:selected { background: #14516a; }"
        )
        self._runtime = runtime
        self._conversations = getattr(runtime, "conversations", None) if runtime else None
        self._preferences_path = preferences_path
        self._on_preferences_saved = on_preferences_saved
        self._diagnostics_provider = diagnostics_provider
        self._is_busy = is_busy
        self._on_session_deleted = on_session_deleted
        self._preferences = normalize_hud_preferences(preferences)
        self._diagnostics_thread: threading.Thread | None = None
        self.diagnostics_ready.connect(
            self._show_diagnostics_result, Qt.ConnectionType.QueuedConnection
        )

        root = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_history_tab(), "Histórico")
        self.tabs.addTab(self._build_diagnostics_tab(), "Diagnóstico")
        self.tabs.addTab(self._build_preferences_tab(), "Preferências")
        self.tabs.setCurrentIndex(0)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        close_button = QPushButton("Fechar", self)
        close_button.clicked.connect(self.close)
        bottom.addWidget(close_button)
        root.addLayout(bottom)
        self._load_history()
        self.refresh_diagnostics()

    def _build_history_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QHBoxLayout(page)
        left = QVBoxLayout()
        self.history_list = QListWidget(page)
        self.history_list.currentItemChanged.connect(self._show_selected_session)
        left.addWidget(QLabel("Conversas salvas neste computador", page))
        left.addWidget(self.history_list, 1)
        refresh_button = QPushButton("Atualizar", page)
        refresh_button.clicked.connect(self._load_history)
        delete_button = QPushButton("Excluir sessão", page)
        delete_button.clicked.connect(self._delete_selected_session)
        clear_button = QPushButton("Limpar todo o histórico", page)
        clear_button.clicked.connect(self._clear_history)
        left.addWidget(refresh_button)
        left.addWidget(delete_button)
        left.addWidget(clear_button)
        right = QVBoxLayout()
        right.addWidget(QLabel("Mensagens", page))
        self.history_view = QTextBrowser(page)
        self.history_view.setPlaceholderText("Selecione uma conversa para ler as mensagens.")
        right.addWidget(self.history_view, 1)
        layout.addLayout(left, 2)
        layout.addLayout(right, 5)
        return page

    def _load_history(self) -> None:
        self.history_list.clear()
        self.history_view.clear()
        if self._conversations is None:
            self.history_list.addItem("Histórico indisponível: runtime não iniciado.")
            return
        try:
            sessions = self._conversations.sessions(limit=100)
            for session in sessions:
                session_id = str(session.get("id") or "")
                messages = self._conversations.messages(session_id)
                first_user = next((m.content.strip() for m in messages if m.role == "user" and m.content.strip()), "")
                title = str(session.get("title") or "").strip() or first_user or "Conversa sem título"
                title = " ".join(title.split())
                if len(title) > 68:
                    title = title[:65].rstrip() + "…"
                started = str(session.get("started_at") or "").replace("T", " ")
                stamp = started[:16] if started else "data desconhecida"
                item = QListWidgetItem(f"{stamp}  ·  {title}")
                item.setData(Qt.ItemDataRole.UserRole, session_id)
                self.history_list.addItem(item)
            if self.history_list.count():
                self.history_list.setCurrentRow(0)
            else:
                self.history_view.setPlainText("Ainda não há conversas salvas.")
        except Exception as exc:
            self.history_view.setPlainText(f"Não foi possível ler o histórico: {type(exc).__name__}: {exc}")

    def _show_selected_session(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None = None) -> None:
        if current is None or self._conversations is None:
            return
        session_id = current.data(Qt.ItemDataRole.UserRole)
        if not session_id:
            self.history_view.clear()
            return
        try:
            messages = self._conversations.messages(str(session_id))
            chunks: list[str] = []
            for message in messages:
                if message.role not in {"user", "assistant"}:
                    continue
                speaker = "Você" if message.role == "user" else "Aurora"
                chunks.append(f"{speaker}  ·  {message.created_at}\n{message.content}")
            self.history_view.setPlainText(("\n\n" + "─" * 48 + "\n\n").join(chunks) if chunks else "Esta conversa não possui mensagens.")
            self.history_view.moveCursor(QTextCursor.MoveOperation.Start)
        except Exception as exc:
            self.history_view.setPlainText(f"Não foi possível abrir a conversa: {type(exc).__name__}: {exc}")

    def _busy_message(self) -> bool:
        try:
            return bool(self._is_busy())
        except Exception:
            return True

    def _delete_selected_session(self) -> None:
        if self._conversations is None:
            return
        if self._busy_message():
            QMessageBox.warning(self, "Tarefa em andamento", "Espere a tarefa atual terminar antes de excluir conversas.")
            return
        item = self.history_list.currentItem()
        session_id = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        if not session_id:
            return
        answer = QMessageBox.question(
            self, "Excluir conversa", "Excluir esta conversa e suas mensagens salvas?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._conversations.delete_session(session_id)
            self._on_session_deleted(session_id)
            self._load_history()
        except Exception as exc:
            QMessageBox.critical(self, "Falha ao excluir", f"Não foi possível excluir: {type(exc).__name__}: {exc}")

    def _clear_history(self) -> None:
        if self._conversations is None:
            return
        if self._busy_message():
            QMessageBox.warning(self, "Tarefa em andamento", "Espere a tarefa atual terminar antes de limpar o histórico.")
            return
        if not self.history_list.count():
            return
        answer = QMessageBox.question(
            self, "Limpar histórico",
            "Apagar todas as conversas e mensagens locais? Memórias semânticas e auditoria serão preservadas.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._conversations.clear_sessions()
            self._on_session_deleted(None)
            self._load_history()
        except Exception as exc:
            QMessageBox.critical(self, "Falha ao limpar", f"Não foi possível limpar: {type(exc).__name__}: {exc}")

    def _build_diagnostics_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("Diagnóstico de conectividade e configuração", page))
        self.diagnostics_view = QTextBrowser(page)
        layout.addWidget(self.diagnostics_view, 1)
        self.refresh_diagnostics_button = QPushButton("Verificar serviços novamente", page)
        self.refresh_diagnostics_button.clicked.connect(self.refresh_diagnostics)
        layout.addWidget(self.refresh_diagnostics_button)
        return page

    def refresh_diagnostics(self) -> None:
        if self._diagnostics_thread is not None and self._diagnostics_thread.is_alive():
            return
        try:
            info = self._diagnostics_provider() or {}
        except Exception as exc:
            info = {"summary": f"Não foi possível coletar a configuração: {type(exc).__name__}: {exc}", "endpoints": []}
        summary = str(info.get("summary") or "Nenhuma informação de diagnóstico disponível.")
        endpoints = list(info.get("endpoints") or [])
        self.diagnostics_view.setPlainText(summary + "\n\nVerificando portas de rede em segundo plano…")
        self.refresh_diagnostics_button.setEnabled(False)

        def worker() -> None:
            checks: list[str] = []
            for endpoint in endpoints:
                label = str(endpoint.get("label") or "Serviço")
                url = str(endpoint.get("url") or "")
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    checks.append(f"{label}: URL não configurada ou inválida.")
                    continue
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                try:
                    with socket.create_connection((parsed.hostname, port), timeout=1.2):
                        checks.append(f"{label}: porta acessível ({parsed.hostname}:{port}).")
                except Exception as exc:
                    checks.append(f"{label}: indisponível ({type(exc).__name__}).")
            self.diagnostics_ready.emit((summary, checks))

        self._diagnostics_thread = threading.Thread(
            target=worker, name="AuroraDiagnostics", daemon=True
        )
        self._diagnostics_thread.start()

    def _show_diagnostics_result(self, payload: object) -> None:
        try:
            summary, checks = payload
            text = str(summary)
            if checks:
                text += "\n\nConectividade\n" + "\n".join(f"• {item}" for item in checks)
            else:
                text += "\n\nNenhum endpoint configurado para teste de conectividade."
            self.diagnostics_view.setPlainText(text)
        except Exception as exc:
            self.diagnostics_view.setPlainText(f"Falha ao exibir diagnóstico: {type(exc).__name__}: {exc}")
        self.refresh_diagnostics_button.setEnabled(True)

    def _build_preferences_tab(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        form = QFormLayout()
        self.speech_enabled_check = QCheckBox("A Aurora deve falar as respostas", page)
        self.streaming_enabled_check = QCheckBox("Mostrar o texto enquanto é gerado", page)
        self.long_panel_check = QCheckBox("Abrir o painel lateral automaticamente para respostas longas", page)
        self.volume_spin = QSpinBox(page)
        self.volume_spin.setRange(25, 125)
        self.volume_spin.setSingleStep(5)
        self.volume_spin.setSuffix("% do volume-base")
        self.rate_spin = QSpinBox(page)
        self.rate_spin.setRange(100, 220)
        self.rate_spin.setSuffix(" palavras/min (SAPI)")
        self.threshold_spin = QSpinBox(page)
        self.threshold_spin.setRange(160, 600)
        self.threshold_spin.setSuffix(" caracteres")
        form.addRow("Fala", self.speech_enabled_check)
        form.addRow("Texto", self.streaming_enabled_check)
        form.addRow("Respostas longas", self.long_panel_check)
        form.addRow("Volume", self.volume_spin)
        form.addRow("Velocidade", self.rate_spin)
        form.addRow("Abrir painel após", self.threshold_spin)
        layout.addLayout(form)
        layout.addWidget(QLabel(
            "As preferências são gravadas em data/hud_preferences.json. "
            "A velocidade ajusta SAPI; o volume é aplicado ao áudio sintetizado. "
            "Nenhuma chave de API é salva neste arquivo.", page
        ))
        save_button = QPushButton("Salvar preferências", page)
        save_button.clicked.connect(self._save_preferences)
        layout.addWidget(save_button)
        layout.addStretch(1)
        self.set_preferences(self._preferences)
        return page

    def set_preferences(self, preferences: Mapping[str, Any]) -> None:
        self._preferences = normalize_hud_preferences(preferences)
        if not hasattr(self, "speech_enabled_check"):
            return
        self.speech_enabled_check.setChecked(self._preferences["speech_enabled"])
        self.streaming_enabled_check.setChecked(self._preferences["streaming_enabled"])
        self.long_panel_check.setChecked(self._preferences["long_responses_in_panel"])
        self.volume_spin.setValue(self._preferences["speech_volume_percent"])
        self.rate_spin.setValue(self._preferences["speech_rate"])
        self.threshold_spin.setValue(self._preferences["response_panel_threshold"])

    def _save_preferences(self) -> None:
        values = {
            "speech_enabled": self.speech_enabled_check.isChecked(),
            "streaming_enabled": self.streaming_enabled_check.isChecked(),
            "long_responses_in_panel": self.long_panel_check.isChecked(),
            "speech_volume_percent": self.volume_spin.value(),
            "speech_rate": self.rate_spin.value(),
            "response_panel_threshold": self.threshold_spin.value(),
        }
        try:
            saved = self._on_preferences_saved(values)
            self._preferences = normalize_hud_preferences(saved if isinstance(saved, dict) else values)
            self.set_preferences(self._preferences)
            QMessageBox.information(
                self, "Preferências salvas",
                "Configurações aplicadas. Algumas alterações de voz só afetam as próximas frases.",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Falha ao salvar", f"Não foi possível aplicar as preferências: {type(exc).__name__}: {exc}")
