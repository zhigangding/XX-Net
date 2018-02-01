import time
import simple_queue

import simple_http_client
from config import config
from xlog import getLogger
xlog = getLogger("cloudflare_front")


show_state_debug = config.getint("system", "show_state_debug", 0)


class Task(object):
    def __init__(self, method, host, path, headers, body, queue, url, timeout):
        self.method = method
        self.host = host
        self.path = path
        self.headers = headers
        self.body = body
        self.queue = queue
        self.url = url
        self.timeout = timeout
        self.start_time = time.time()
        self.unique_id = "%s:%f" % (url, self.start_time)
        self.trace_time = []
        self.body_queue = simple_queue.Queue()
        self.body_len = 0
        self.body_readed = 0
        self.content_length = None
        self.worker = None
        self.read_buffer = ""
        self.responsed = False
        self.finished = False
        self.retry_count = 0

    def to_string(self):
        out_str = " Task:%s\r\n" % self.url
        out_str += "   responsed:%d" % self.responsed
        out_str += "   retry_count:%d" % self.retry_count
        out_str += "   start_time:%d" % (time.time() - self.start_time)
        out_str += "   body_readed:%d\r\n" % self.body_readed
        out_str += "   Trace:%s" % self.get_trace()
        out_str += "\r\n"
        return out_str

    def put_data(self, data):
        self.body_queue.put(data)
        self.body_len += len(data)

    def read(self, size=None):
        # fail or cloe if return ""
        if self.body_readed == self.content_length:
            return ""

        if size:
            while len(self.read_buffer) < size:
                data = self.body_queue.get(self.timeout)
                if not data:
                    return ""

                self.read_buffer += data

            data = self.read_buffer[:size]
            self.read_buffer = self.read_buffer[size:]
        else:
            if len(self.read_buffer):
                data = self.read_buffer
                self.read_buffer = ""
            else:
                data = self.body_queue.get(self.timeout)
                if not data:
                    return ""

        self.body_readed += len(data)
        return data

    def read_all(self):
        out_list = []
        while True:
            data = self.body_queue.get(self.timeout)
            if not data:
                break
            out_list.append(data)

        return "".join(out_list)

    def set_state(self, stat):
        # for debug trace
        time_now = time.time()
        self.trace_time.append((time_now, stat))
        if show_state_debug:
            xlog.debug("%s stat:%s", self.unique_id, stat)
        return time_now

    def get_trace(self):
        out_list = []
        last_time = self.start_time
        for t, stat in self.trace_time:
            time_diff = int((t - last_time) * 1000)
            last_time = t
            out_list.append("%d:%s" % (time_diff, stat))
        out_list.append(":%d" % ((time.time()-last_time)*1000))
        return ",".join(out_list)

    def response_fail(self, reason=""):
        if self.responsed:
            xlog.error("http_common responsed_fail but responed.%s", self.url)
            self.put_data("")
            return

        self.responsed = True
        err_text = "response_fail:%s" % reason
        xlog.debug("%s %s", self.url, err_text)
        res = simple_http_client.BaseResponse(body=err_text)
        res.task = self
        res.worker = self.worker
        self.queue.put(res)
        self.finish()

    def finish(self):
        if self.finished:
            return

        self.put_data("")
        self.finished = True


class HTTP_worker(object):
    def __init__(self, ssl_sock, close_cb, retry_task_cb, idle_cb, log_debug_data):
        self.ssl_sock = ssl_sock
        self.init_rtt = ssl_sock.handshake_time / 2
        self.rtt = self.init_rtt
        self.speed = 1
        self.ip = ssl_sock.ip
        self.close_cb = close_cb
        self.retry_task_cb = retry_task_cb
        self.idle_cb = idle_cb
        self.log_debug_data = log_debug_data
        self.accept_task = True
        self.keep_running = True
        self.processed_tasks = 0
        self.speed_history = []
        self.last_active_time = ssl_sock.create_time

    def update_debug_data(self, rtt, sent, received, speed):
        self.rtt = rtt
        self.log_debug_data(rtt, sent, received)
        self.speed = speed
        self.speed_history.append(speed)

    def close(self, reason):
        self.accept_task = False
        self.keep_running = False
        self.ssl_sock.close()
        xlog.debug("%s worker close:%s", self.ip, reason)
        self.close_cb(self)

    def get_score(self):
        now = time.time()
        inactive_time = now - self.last_active_time

        rtt = self.rtt
        if inactive_time > 30 and rtt > 1000:
            rtt = self.rtt = 200

        if self.version != "1.1":
            rtt += len(self.streams) * 100

        if inactive_time < 1:
            score = rtt + 5000
        else:
            score = rtt + (240 - inactive_time)*10

        return score