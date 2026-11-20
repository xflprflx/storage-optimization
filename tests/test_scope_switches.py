"""Posição pela saída (Por endereço e Pool): as opções que dependem do histórico e da planta valem para a filial
escolhida — também quando o horário de liberação só está na amostra (históricos de versões anteriores) e quando a
filial é trocada no escopo (a tela recarrega com o formulário preenchido)."""
import re


def _disabled(html: str, el_id: str) -> bool:
    tag = re.search(rf'<input[^>]*id="{el_id}"[^>]*>', html).group(0)
    return " disabled" in tag


def test_switches_follow_the_branch_history_and_plan(client, db):
    from models import Branch, BranchLayout, PickHistory

    br = Branch(code="SW", name="Interruptores")
    db.add(br)
    db.flush()
    # histórico antigo: horários na amostra, sem a marcação has_release no resumo
    db.add(PickHistory(branch_id=br.id, filename="h.xlsx", data={
        "kind": "orders", "orders": 1, "lines": 1, "skus": {"X": [1, 1]}, "pairs": [],
        "sample": [["P1", ["X"], [1], "2026-10-01", 30000]]}))
    db.add(BranchLayout(branch_id=br.id, warehouse_id=None, data={"rows": [], "obstacles": []}))
    db.commit()
    for page in ("/optimization", "/optimization/pool"):
        html = client.get(f"{page}?branch_id={br.id}").text
        assert not _disabled(html, "slot_release") and not _disabled(html, "sw_slot_rearr")
        none = client.get(page).text  # sem filial (como pelo menu): desabilitadas, e a troca da filial recarrega
        assert _disabled(none, "slot_release") and "Filial ou armazém trocados no escopo" in none
