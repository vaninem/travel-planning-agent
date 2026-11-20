"""E01：一个可解释、自洽的教学计数器，不声称等于任何真实模型 tokenizer。"""

from .errors import ExercisePending

def is_chn(ch: str) -> bool:
    if len(str) != 1:
        return False
    code = ord(ch)
    return 0x4E00 <= code <= 0x9FFF

class ApproxTokenCounter:
    def __init__(self) -> None:
        self.name = "teaching-lexical-v1"

    def count(self, text: str) -> int:
        """估算长度。

        TODO E01：连续 ASCII 字母/数字/下划线是一组，计 1；其他非空白
        字符各计 1（包括汉字、标点），空白计 0。空文本为 0。
        用这个明确规则避免“汉字数 + 原文 split”重复计算汉字。
        例：'你好 world' 为 3，'Hello, world!' 为 4。
        """
        cur_count = 0
        last_word = False
        for ch in text:
            is_word_char = ch.isascii() and (ch.isalnum() or ch == "_")
            if is_word_char:
                if not last_word:
                    cur_count += 1
                    last_word = True
            else:
                if not ch.isspace():
                    cur_count += 1
                last_word = False
        return cur_count
        raise ExercisePending("E01", "实现 ApproxTokenCounter.count")

    def truncate(self, text: str, max_tokens: int) -> str:
        """返回原文本的前缀，按本计数器计数不超预算。

        TODO E01：负预算报 ValueError；0 预算返回空串；原文已放得下时
        原样返回。不可仅按固定字符比例截断后便假定符合 token 预算。
        """
        if max_tokens < 0:
            raise ValueError("max_token不能为负数")
        elif max_tokens == 0:
            return ''
        
        result = ''
        cur_count = 0
        last_word = False
        for ch in text:
            is_word_char = ch.isascii() and (ch.isalnum() or ch == "_")
            if is_word_char:
                if not last_word:
                    if cur_count == max_tokens:
                        return result
                    else:
                        cur_count += 1
                        last_word = True
            else:
                if not ch.isspace():
                    if cur_count == max_tokens:
                        return result
                    cur_count += 1
                last_word = False
            result += ch
        return result
        raise ExercisePending("E01", "实现 ApproxTokenCounter.truncate 并复算预算")
