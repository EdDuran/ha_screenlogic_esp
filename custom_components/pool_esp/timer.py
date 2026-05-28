import traceback
from typing import Protocol

import logging
import asyncio

import homeassistant

_LOG = logging.getLogger(__name__)

class Timer:
    """
    Cancellable duration timer with typed callbacks.
    Supports multiple independent instances — each has its own state.

    Usage:
        timer = DurationTimer(
            name       = "pool_eta",
            context    = pool_context,
            callback   = my_callback,
            duration   = 300,
            interval   = 60
        )
        timer.start()
        timer.stop()
    """

    def __init__(self, name: str, hass:homeassistant, context, callback: TimerCallback, duration:int, interval: int = 1):
        self._hass = hass
        self._context = context
        self._name = name
        self._callback = callback
        self._duration = duration
        self._interval = interval
        self._cycles = duration // interval if interval > 0 else 0

        self._running = False
        self._elapsed = 0
        self._task_name = f"esp_timer_{name}"
        self._task = None

    def __str__(self) -> str:
        return f"Timer[{self._name}]: {"is" if self.is_running else "is Not"} Running, duration[{self._duration}s], interval[{self._interval}s], remaining[{self._duration - self._elapsed}s]"

    @property
    def name(self) -> str:
        return self._name
    
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def elapsed(self) -> int:
        return self._elapsed

    @property
    def context(self) -> int:
        return self._context

    ###
    ### Start Timer
    ###

    def start(self):
        """Start the timer — cancels any previous instance with same name."""
        try:
            if self._running:
                _LOG.info(f"Timer[{self._name}]: already running, restarting")

            if self._task:
                self._task.cancel()

            self._running = True
            self._elapsed = 0
            self._task = self._hass.loop.create_task(self._run())
        except Exception as e:
            _LOG.error(f"...Failed to Start Timer[{self._name}] {e}")

    ###
    ### Stop Timer
    ###
    async def stop(self):
        """Stop the timer — on_timer_cancelled will be called."""
        _LOG.info(f"Timer[{self._name}] STOPPED")

        try:
            if self._task:
                self._task.cancel()
                self._task = None

            if self._running:
                self._running = False
                try:
                    await self._callback.on_timer_cancelled(self)
                except Exception as e:
                    _LOG.error(f"Timer[{self._name}]: on_timer_cancelled() failed: {e}")
        except Exception as e:
            _LOG.error(f"...Failed to Stop Timer[{self._name}] {e}")

    ###
    ### Run Loop
    ###
    async def _run(self):
        """Internal async loop."""
        try:
            continuous = self._duration == 0    # Zero duration means run indefinitely until stopped
            remaining = self._duration

            _LOG.info(f"Timer[{self._name}]: Started — duration[{"continuous" if continuous else self._duration}s] interval[{self._interval}s] ")

            while self._running and (continuous or remaining > 0):
                await asyncio.sleep(self._interval)

                if not self._running:
                    return  # stopped externally during sleep

                self._elapsed += self._interval
                if not continuous:
                    remaining -= self._interval

                try:
                    await self._callback.on_timer_interval(self, self._elapsed, remaining)
                except Exception as e:
                    _LOG.error(f"Timer[{self._name}]: on_timer_interval() failed;{e}")
                    _LOG.error(traceback.format_exc())
            # Natural completion
            self._running = False
            self._task = None
            _LOG.info(f"Timer[{self._name}]: completed {self._elapsed} cycles")
            try:
                await self._callback.on_timer_complete(self)
            except Exception as e:
                _LOG.error(f"Timer[{self._name}]: on_timer_complete() failed; {e}")
        except asyncio.CancelledError:
                _LOG.debug(f"Timer[{self._name}]: cancelled")


class TimerCallback(Protocol):
    """
    Interface for duration timer callbacks.
    Implement this protocol to receive timer events.
    """
    def on_timer_interval(self, timer: Timer, elapsed: int, remaining: int) -> None:
        """Called every cycle."""
        ...

    def on_timer_complete(self, timer: Timer) -> None:
        """Called when timer completes naturally."""
        ...

    def on_timer_cancelled(self, timer: Timer) -> None:
        """Called when timer is stopped externally."""
        ...