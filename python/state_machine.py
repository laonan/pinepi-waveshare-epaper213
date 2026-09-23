class StateMachine:
    """Global page state machine: Page 1(1), Page 2(2), Page 3(3)"""

    def __init__(self):
        self._page = 1  # Start from Page 1
        self._callbacks = []
        self._render_in_progress = False
        self._generation = 0

    @property
    def current_page(self) -> int:
        return self._page

    @property
    def render_in_progress(self) -> bool:
        return self._render_in_progress

    def page_snapshot(self):
        return self._page, self._generation

    def is_current(self, page: int, generation: int) -> bool:
        return self._page == page and self._generation == generation

    def begin_render(self) -> None:
        self._render_in_progress = True

    def end_render(self) -> None:
        self._render_in_progress = False

    def next_page(self) -> int:
        self._page = self._page % 3 + 1  # Cycle: 1 -> 2 -> 3 -> 1
        self._generation += 1
        self._notify()
        return self._page

    def set_page(self, page: int) -> int:
        if 1 <= page <= 3 and page != self._page:
            self._page = page
            self._generation += 1
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
