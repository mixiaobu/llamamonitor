# §40 测试代理：9092 -> 9091；/metrics 加 3s 延迟（其余直通）
import socket
import threading
import time

LISTEN = 9092
UPSTREAM = "127.0.0.1", 9091
DELAY_PATH = b"/metrics"


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle(conn):
    try:
        # 读请求头
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        head = buf.split(b"\r\n\r\n", 1)[0]
        first = head.split(b"\r\n", 1)[0].decode("latin-1")
        is_metrics = first.startswith("GET ") and (
            first.split(" ")[1].split("?", 1)[0] == DELAY_PATH.decode())
        up = socket.create_connection(UPSTREAM, timeout=10)
        up.sendall(buf)
        if is_metrics:
            time.sleep(3.0)  # §40：prometheus 慢 3s
        t_in = threading.Thread(target=pipe, args=(up, conn), daemon=True)
        t_out = threading.Thread(target=pipe, args=(conn, up), daemon=True)
        t_in.start()
        t_out.start()
        t_in.join()
        t_out.join()
        up.close()
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", LISTEN))
    srv.listen(16)
    print("slow proxy listening on 127.0.0.1:%d (-> 9091, /metrics +3s)" % LISTEN, flush=True)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
