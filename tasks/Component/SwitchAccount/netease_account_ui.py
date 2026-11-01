from __future__ import annotations

import re
import time
from dataclasses import dataclass

from lxml import etree

from module.logger import logger


class AccountUiUnavailable(RuntimeError):
    """无法通过 UIAutomator 获取网易登录账号控件树。"""


@dataclass(frozen=True)
class AccountUiEntry:
    """网易账号下拉列表中的一个可点击账号项。"""

    account: str
    bounds: tuple[int, int, int, int]


class NeteaseAccountUi:
    """通过 UIAutomator 控件树读取和操作网易已保存账号列表。"""

    ACCOUNT_ITEM_ID = "netease_mpay__login_user_item"
    ACCOUNT_LIST_ID = "netease_mpay__user_list"
    # “添加新账号”只会出现在账号列表底部，可作为可靠的到底标志。
    ADD_ACCOUNT_ID = "netease_mpay__add_user"
    ACCOUNT_TEXT_IDS = (
        "netease_mpay__login_username_with_tag",
        "netease_mpay__login_username",
    )

    def __init__(self, device, *, settle_seconds: float = 0.5, max_scrolls: int = 12):
        self.device = device
        self.settle_seconds = settle_seconds
        self.max_scrolls = max_scrolls

    @staticmethod
    def normalize_account(account: str | None) -> str:
        return "".join((account or "").split()).casefold()

    @staticmethod
    def _resource_xpath(resource_id: str) -> str:
        length = len(resource_id)
        return (
            "//*["
            f"substring(@resource-id, string-length(@resource-id) - {length - 1}) = \"{resource_id}\""
            "]"
        )

    @staticmethod
    def _bounds(node) -> tuple[int, int, int, int]:
        values = [int(value) for value in re.findall(r"\d+", node.attrib.get("bounds", ""))]
        if len(values) != 4:
            raise AccountUiUnavailable("账号控件坐标无效")
        left, top, right, bottom = values
        if right <= left or bottom <= top:
            raise AccountUiUnavailable("账号控件坐标为空")
        return left, top, right, bottom

    def dump(self):
        """读取网易登录弹窗的多窗口控件树。"""
        last_error = None
        for attempt in range(2):
            try:
                content = self.device.u2.dump_hierarchy(compressed=False, pretty=False)
                return etree.fromstring(content.encode("utf-8"))
            except Exception as exc:
                last_error = exc
                if attempt == 0:
                    try:
                        self.device.u2.uiautomator.start()
                        time.sleep(self.settle_seconds)
                    except Exception as start_exc:
                        last_error = start_exc
        raise AccountUiUnavailable(f"读取网易账号控件树失败：{last_error}") from last_error

    def _nodes(self, root, resource_id: str):
        return root.xpath(self._resource_xpath(resource_id))

    def current_account(self, root=None) -> str | None:
        """读取下拉框关闭时当前已选中的账号。"""
        root = root if root is not None else self.dump()
        # 下拉列表展开时会存在多条账号文本，不能误判为当前账号。
        if self._nodes(root, self.ACCOUNT_LIST_ID):
            return None
        for resource_id in self.ACCOUNT_TEXT_IDS:
            for node in self._nodes(root, resource_id):
                text = node.attrib.get("text", "").strip()
                if text:
                    return text
        return None

    def account_matches(self, actual: str | None, expected: str) -> bool:
        return self.normalize_account(actual) == self.normalize_account(expected)

    def nodes(self, root, resource_id: str):
        """查找带指定资源 ID 后缀的控件节点。"""
        return self._nodes(root, resource_id)

    def clickable_node(self, root, resource_id: str):
        """返回指定资源 ID 中第一个可见可点击控件。"""
        return next(
            (node for node in self._nodes(root, resource_id)
             if node.attrib.get("clickable") == "true"
             and node.attrib.get("enabled") == "true"),
            None,
        )

    def click_node(self, node, control_name: str) -> bool:
        """点击已从当前控件树中获取的节点。"""
        try:
            left, top, right, bottom = self._bounds(node)
            self.device.click(
                (left + right) // 2,
                (top + bottom) // 2,
                control_name=control_name,
            )
            return True
        except Exception:
            return False

    def click_resource(self, resource_id: str, control_name: str) -> bool:
        """按控件树资源 ID 点击控件并返回是否成功。"""
        try:
            root = self.dump()
            node = self.clickable_node(root, resource_id)
            return self.click_node(node, control_name) if node is not None else False
        except Exception:
            return False

    def current_user_center_account(self, root=None) -> str | None:
        """读取用户中心页面当前选中的网易账号。"""
        try:
            root = root if root is not None else self.dump()
            for node in self._nodes(root, "netease_mpay__login_center_username"):
                text = node.attrib.get("text", "").strip()
                if text:
                    return text
        except Exception:
            return None
        return None

    def _click_bounds(self, bounds: tuple[int, int, int, int], name: str) -> None:
        left, top, right, bottom = bounds
        self.device.click(
            (left + right) // 2,
            (top + bottom) // 2,
            control_name=name,
        )

    def _account_entries(self, root) -> list[AccountUiEntry]:
        list_nodes = self._nodes(root, self.ACCOUNT_LIST_ID)
        if not list_nodes:
            return []
        list_node = list_nodes[0]
        entries: list[AccountUiEntry] = []
        for resource_id in self.ACCOUNT_TEXT_IDS:
            for text_node in list_node.xpath("." + self._resource_xpath(resource_id)):
                account = text_node.attrib.get("text", "").strip()
                if not account:
                    continue
                clickable = next(
                    (node for node in text_node.iterancestors() if node.attrib.get("clickable") == "true"),
                    text_node,
                )
                if (text_node.attrib.get("enabled") == "false"
                        or text_node.attrib.get("visible-to-user") == "false"):
                    continue
                left, top, right, bottom = self._bounds(clickable)
                ll, lt, lr, lb = self._bounds(list_node)
                bounds = (max(left, ll), max(top, lt), min(right, lr), min(bottom, lb))
                if bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                    entries.append(AccountUiEntry(account, bounds))
        return entries

    def _open_list(self, root) -> bool:
        if self._nodes(root, self.ACCOUNT_LIST_ID):
            return True
        candidates = [
            node for node in self._nodes(root, self.ACCOUNT_ITEM_ID)
            if node.attrib.get("clickable") == "true"
        ]
        if len(candidates) != 1:
            return False
        self._click_bounds(self._bounds(candidates[0]), "网易账号列表展开")
        time.sleep(self.settle_seconds)
        return True

    def _at_list_bottom(self, root) -> bool:
        """“添加新账号”出现时，说明账号列表已经滑到底部。"""
        return bool(self._nodes(root, self.ADD_ACCOUNT_ID))

    def _scroll(self, root, *, toward_start: bool) -> bool:
        list_nodes = self._nodes(root, self.ACCOUNT_LIST_ID)
        if not list_nodes:
            return False
        left, top, right, bottom = self._bounds(list_nodes[0])
        x = (left + right) // 2
        height = bottom - top
        if toward_start:
            start = (x, top + height // 3)
            end = (x, bottom - height // 5)
            name = "netease_account_list_to_start"
        else:
            start = (x, bottom - height // 5)
            end = (x, top + height // 3)
            name = "netease_account_list_to_end"
        self.device.swipe(start, end, duration=0.35, control_name=name)
        # 完整遍历账号列表可能需要反复双向滑动。这是预期行为，不应
        # 被全局“两个按钮各点击六次”的防卡死规则误判。
        remove_record = getattr(self.device, "click_record_remove", None)
        if callable(remove_record):
            remove_record(name)
        time.sleep(self.settle_seconds)
        return True

    def _list_signature(self, root):
        # 同时比较账号和位置：账号名称相同，不代表列表已经停止惯性滚动。
        return tuple((self.normalize_account(e.account), e.bounds)
                     for e in self._account_entries(root))

    def _wait_list_stable(self, root):
        previous = self._list_signature(root)
        unchanged = 0
        for _ in range(12):
            time.sleep(max(0.2, self.settle_seconds))
            root = self.dump()
            signature = self._list_signature(root)
            if not signature:
                return root
            unchanged = unchanged + 1 if signature == previous else 0
            if unchanged >= 2:
                return root
            previous = signature
        raise AccountUiUnavailable("账号列表持续移动，无法安全点击目标账号")

    def _select_visible(self, root, target: str, matcher) -> bool:
        if not any(matcher(e.account) for e in self._account_entries(root)):
            return False
        # 列表惯性滚动期间，点击可能只会停止滚动，并未选中账号。
        # 等列表停稳后重新定位目标行，原地重试，不要立即滑走。
        for attempt in range(3):
            root = self._wait_list_stable(root)
            current = self.current_account(root)
            if current is not None:
                return bool(matcher(current))
            entry = next((e for e in self._account_entries(root) if matcher(e.account)), None)
            if entry is None:
                return False
            self._click_bounds(entry.bounds, "网易账号选择")
            for _ in range(4):
                time.sleep(self.settle_seconds)
                root = self.dump()
                if matcher(self.current_account(root)):
                    return True
            if not self._nodes(root, self.ACCOUNT_LIST_ID):
                return False
            logger.warning("目标账号点击后尚未选中，第 %s/3 次确认", attempt + 1)
        raise AccountUiUnavailable("已找到目标账号，但三次点击后仍未确认选中")

    def _scan_to_edge(self, target: str, matcher, *, toward_start: bool) -> bool:
        """列表停稳后逐行扫描，仅在确认到达边界时结束，避免惯性滚动时误判。"""
        previous = None
        for _ in range(self.max_scrolls):
            root = self._wait_list_stable(self.dump())
            if matcher(self.current_account(root)):
                return True
            entries = self._account_entries(root)
            signature = self._list_signature(root)
            if not entries:
                return False
            if self._select_visible(root, target, matcher):
                return True
            if signature == previous or (not toward_start and self._at_list_bottom(root)):
                return False
            previous = signature
            if not self._scroll(root, toward_start=toward_start):
                return False
        return False

    def _reopen_list(self) -> bool:
        """收起并重新展开账号列表，使 PopupWindow 和控件树重新创建。"""
        root = self.dump()
        list_nodes = self._nodes(root, self.ACCOUNT_LIST_ID)
        if list_nodes:
            candidates = []
            for node in self._nodes(root, self.ACCOUNT_ITEM_ID):
                if node.attrib.get("clickable") != "true":
                    continue
                inside_list = any(
                    ancestor.attrib.get("resource-id", "").endswith(self.ACCOUNT_LIST_ID)
                    for ancestor in node.iterancestors()
                )
                if not inside_list:
                    candidates.append(node)
            if len(candidates) == 1:
                self._click_bounds(self._bounds(candidates[0]), "网易账号列表收起")
            else:
                # 当前版本的登录组件按返回键会关闭整个登录窗口。
                # 仅点击控件树确认的下拉列表上方窗口区域来收起列表，
                # 不使用猜测的游戏画面坐标。
                contents = self._nodes(root, "netease_mpay__activity_content")
                if len(contents) != 1:
                    return False
                left, top, right, bottom = self._bounds(contents[0])
                ll, lt, lr, lb = self._bounds(list_nodes[0])
                if lt - top < 20 or not (left <= ll < lr <= right):
                    return False
                self._click_bounds((ll, top, lr, lt), "网易账号列表收起")
            time.sleep(max(1.0, self.settle_seconds))

        root = self.dump()
        if self._nodes(root, self.ACCOUNT_LIST_ID):
            logger.warning("网易账号列表收起后仍然可见")
            return False
        if not self._open_list(root):
            logger.warning("网易账号列表重新展开失败")
            return False
        time.sleep(max(1.0, self.settle_seconds))
        return True

    def select_account(self, target: str, matcher=None) -> bool:
        """参考项目搜索顺序，并在失败时进行双向复查和刷新重试。"""
        matcher = matcher or (lambda actual: self.account_matches(actual, target))
        root = self.dump()
        if matcher(self.current_account(root)):
            return True
        if not self._open_list(root):
            return False

        root = self.dump()
        if self._select_visible(root, target, matcher):
            return True

        # 参考项目核心：先归位顶部，再从顶部完整扫描到底部。
        if self._scan_to_edge(target, matcher, toward_start=True):
            return True
        if self._scan_to_edge(target, matcher, toward_start=False):
            return True

        # 第一轮未找到：从底部反向完整扫描回顶部。
        logger.info("网易账号首次扫描未找到目标，从底部向顶部复查")
        if self._scan_to_edge(target, matcher, toward_start=True):
            return True

        # 双向均未找到时重新创建列表 PopupWindow，等待控件树刷新，
        # 然后再执行一次“顶部到达底部”的最终完整扫描。
        logger.info("网易账号双向扫描未找到目标，重新展开列表后最终重试")
        if not self._reopen_list():
            return False
        if self._scan_to_edge(target, matcher, toward_start=True):
            return True
        if self._scan_to_edge(target, matcher, toward_start=False):
            return True

        logger.warning("网易账号三轮完整扫描后仍未找到目标")
        return False
