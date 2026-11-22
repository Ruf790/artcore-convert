"""2.2.2: no «Help» menu; the A–B segment from ArtCore Player — its message comes from i18n, a busy file says so."""
import os

from conftest import pump
from test_20 import _win


def test_no_help_menu(qapp, data_dir):
    from convert.i18n import t
    w = _win(qapp)
    titles = [a.text() for a in w.menuBar().actions()]
    assert "Справка" not in titles and "Help" not in titles
    acts = [a.text() for a in w.menuBar().actions()[0].menu().actions()]
    assert t("about") in acts                                  # «About» stays in the program's own menu
    w.close()


def test_clip_from_player(qapp, media, data_dir, tmp_path):
    from convert.i18n import t
    w = _win(qapp, out_mode="folder", out_dir=str(tmp_path))
    src = os.path.join(media, "Movie (2017).mkv")
    w.open_from_shell([src], "", False, (1.0, 2.5))
    j = w.jobs[0]
    assert pump(qapp, 20, lambda: j.status == "wait")
    assert j.trim == [[1.0, 2.5]]
    from convert import job as J
    assert w.statusBar().currentMessage() == t("clip_set", J.fmt_tc(1.0), J.fmt_tc(2.5))
    # the same file while it is being encoded: nothing changes, and the reason is shown
    w._busy = lambda x: x is j                                 # as if its encode were running
    w._apply_clip(src, (3.0, 4.0))
    assert j.trim == [[1.0, 2.5]]
    assert w.statusBar().currentMessage() == t("clip_busy", os.path.basename(src))
    w.close()
