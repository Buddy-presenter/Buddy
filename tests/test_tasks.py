from PySide6.QtCore import QThread

from buddy_desktop.tasks import TaskRunner


def test_unexpected_failure_is_redacted_and_delivered_on_gui_thread(qtbot, qapp):
    runner = TaskRunner()
    delivered_threads = []
    runner.failed.connect(lambda *_: delivered_threads.append(QThread.currentThread()))

    def fail():
        raise ValueError("secret provider payload")

    with qtbot.waitSignal(runner.failed, timeout=3000) as signal:
        runner.submit(fail)
    assert "secret" not in signal.args[1]
    assert delivered_threads == [qapp.thread()]
    qtbot.waitUntil(lambda: not runner.busy)
    runner.wait_for_shutdown()
