
class Exit(SystemExit):
    def __init__(self, failures: int):
        m = f"{failures} test failed" if failures == 1 else f"{failures} tests failed"
        super().__init__(m)

