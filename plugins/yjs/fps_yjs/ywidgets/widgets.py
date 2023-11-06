import pkg_resources
from pycrdt import TransactionEvent
from ypywidgets.utils import (
    YMessageType,
    YSyncMessageType,
    create_update_message,
    process_sync_message,
    sync,
)


class Widgets:
    def __init__(self):
        self.ydocs = {
            ep.name: ep.load() for ep in pkg_resources.iter_entry_points(group="ypywidgets")
        }
        self.widgets = {}

    def comm_open(self, msg, comm) -> None:
        target_name = msg["content"]["target_name"]
        if target_name != "ywidget":
            return

        name = msg["metadata"]["ymodel_name"]
        comm_id = msg["content"]["comm_id"]
        self.comm = comm
        model = self.ydocs[name](primary=False)
        self.widgets[comm_id] = {"model": model, "comm": comm}
        msg = sync(model.ydoc)
        comm.send(**msg)

    def comm_msg(self, msg) -> None:
        comm_id = msg["content"]["comm_id"]
        message = bytes(msg["buffers"][0])
        if message[0] == YMessageType.SYNC:
            ydoc = self.widgets[comm_id]["model"].ydoc
            reply = process_sync_message(
                message[1:],
                ydoc,
            )
            if reply:
                self.widgets[comm_id]["comm"].send(buffers=[reply])
            if message[1] == YSyncMessageType.SYNC_STEP2:
                ydoc.observe(self._send)

    def _send(self, event: TransactionEvent):
        update = event.get_update()
        message = create_update_message(update)
        try:
            self.comm.send(buffers=[message])
        except Exception:
            pass
