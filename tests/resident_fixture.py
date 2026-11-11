"""Subprocess fixture: production lifecycle with a tiny dictionary, no large load."""

import time
from unittest.mock import patch

from oyayubi.ime import resident, server
from oyayubi.ime.session import ImeSession
from tools.wp0.mozc_dict import MozcLex
from tools.wp0.table import load_table


def main():
    parser = server.argument_parser()
    parser.add_argument("--load-delay", type=float, default=0)
    args = parser.parse_args()

    def load(args):
        with (args.runtime_dir / "loads").open("a") as stream:
            stream.write("load\n")
        time.sleep(args.load_delay)
        return ImeSession(_table=load_table(), _lex=MozcLex({}, set(), [0] * 4, 2),
                          learning_path=None)

    with patch.object(resident, "load_session", side_effect=load):
        resident.run_resident(args)


if __name__ == "__main__":
    main()
