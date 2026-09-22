#!/usr/bin/env python3
"""ローカルの閲覧サーバを起動する。

    python3 serve.py            起動してブラウザを開く
    python3 serve.py --no-open  ブラウザは開かない
"""

import sys
import threading
import webbrowser

import gline
from gline import config, server


def main(argv):
    gline.use_utf8_output()
    try:
        cfg = config.load()
    except config.ConfigError as exc:
        config.die(str(exc))

    httpd = server.serve(cfg)
    url = "http://127.0.0.1:%d/" % cfg.port
    print("起動しました: %s" % url)
    print("止めるときは Ctrl-C。")

    if "--no-open" not in argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n終了します。")
    finally:
        server.shutdown(httpd)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
