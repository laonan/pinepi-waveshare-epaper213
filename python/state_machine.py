class StateMachine:
    """Global page state machine: Page 1(1), Page 2(2), Page 3(3)"""

    def __init__(self):
        self._page = 1  # Start from Page 1
        self._callbacks = []

    @property
    def current_page(self) -> int:
        return self._page

    def next_page(self) -> int:
        self._page = self._page % 3 + 1  # Cycle: 1 -> 2 -> 3 -> 1
        self._notify()
        return self._page

    def set_page(self, page: int) -> int:
        if 1 <= page <= 3:
            self._page = page
            self._notify()
        return self._page

    def on_change(self, callback):
        self._callbacks.append(callback)

    def _notify(self):
        for cb in self._callbacks:
            try:
                cb(self._page)
            except Exception as e:
                print(f"[StateMachine] callback error: {e}")
