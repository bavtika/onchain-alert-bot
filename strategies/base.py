from abc import ABC, abstractmethod


class BaseStrategy(ABC):
    def __init__(self, providers: dict, notifier, db_module):
        self.providers = providers
        self.notifier = notifier
        self.db = db_module

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def run(self) -> None: ...
