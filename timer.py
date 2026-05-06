from typing import Protocol

import logging
import asyncio

_LOGGER = logging.getLogger(__name__)

class Timer:
    """
    Cancellable duration timer with typed callbacks.
    Supports multiple independent instances — each has its own state.

    Usage:
        timer = DurationTimer(
            name       = "pool_eta",
            context    = pool_context,
            callback   = my_callback,
            cycle      = 30,
            interval   = 60
        )
        timer.start()
        timer.stop()
    """

    def __init__(self, name: str, context: Context, callback: TimerCallback, cycles: int = 1, interval: int = 60):
        self._hass = context.coordinator.hass
        self._name = name
        self._context = context
        self._callback = callback
        self._cycles = cycles
        self._interval = interval

        self._running = False
        self._elapsed = 0
        self._task_name = f"esp_timer_{name}_{context.body_type}"
        self._task = None

    def __str__(self) -> str:
        return f"Timer[{self._name}] {"is" if self.is_running else "is Not"} Running"

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def elapsed(self) -> int:
        return self._elapsed

    @property
    def context(self) -> Context:
        return self._context

    ###
    ### Start Timer
    ###

    def start(self):
        """Start the timer — cancels any previous instance with same name."""
        _LOGGER.info(f"Timer[{self._name}] STARTED, [{self._cycles}] cycles of [{self._interval}s] each")

        try:
            if self._running:
                _LOGGER.info(f"Timer[{self._name}]: already running, restarting")

            if self._task:
                self._task.cancel()

            self._running = True
            self._elapsed = 0
            self._task = self._hass.loop.create_task(self._run())
            # self._task = asyncio.create_task(self._run()) # Alternate without needing coordinator passed in
        except Exception as e:
            _LOGGER.error(f"...Failed to Start Timer[{self._name}] {e}")

    ###
    ### Stop Timer
    ###
    async def stop(self):
        """Stop the timer — on_timer_cancelled will be called."""
        _LOGGER.info(f"Timer[{self._name}] STOPPED")

        try:
            if self._task:
                self._task.cancel()
                self._task = None

            if self._running:
                self._running = False
                try:
                    await self._callback.on_timer_cancelled(self)
                except Exception as e:
                    _LOGGER.error(f"Timer[{self._name}]: on_timer_cancelled() failed: {e}")
        except Exception as e:
            _LOGGER.error(f"...Failed to Stop Timer[{self._name}] {e}")

    ###
    ### Run Loop
    ###
    async def _run(self):
        """Internal async loop."""
        try:
            continuous = self._cycles == 0
            remaining = self._cycles

            _LOGGER.info(f"Timer[{self._name}]: Started — interval={self._interval}s count={"endless" if continuous else self._cycles}")

            while self._running and (continuous or remaining > 0):
                await asyncio.sleep(self._interval)

                if not self._running:
                    return  # stopped externally during sleep

                self._elapsed += 1
                if not continuous:
                    remaining -= 1

                try:
                    await self._callback.on_timer_cycle(self, self._elapsed, remaining)
                except Exception as e:
                    _LOGGER.error(f"Timer[{self._name}]: on_timer_cycle() failed;{e}")

            # Natural completion
            self._running = False
            self._task = None
            _LOGGER.info(f"Timer[{self._name}]: completed {self._elapsed} cycles")
            try:
                await self._callback.on_timer_complete(self)
            except Exception as e:
                _LOGGER.error(f"Timer[{self._name}]: on_timer_complete() failed; {e}")
        except asyncio.CancelledError:
                _LOGGER.debug(f"Timer[{self._name}]: cancelled")


class TimerCallback(Protocol):
    """
    Interface for duration timer callbacks.
    Implement this protocol to receive timer events.
    """
    def on_timer_cycle(self, timer: Timer, elapsed: int, remaining: int) -> None:
        """Called every cycle."""
        ...

    def on_timer_complete(self, timer: Timer) -> None:
        """Called when timer completes naturally."""
        ...

    def on_timer_cancelled(self, timer: Timer) -> None:
        """Called when timer is stopped externally."""
        ...