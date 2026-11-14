"""PR #149 獨立審查（2026-11-14）之後的修正與補強。所有 token、密碼、金鑰都是合成值。

審查（redaction-149-zh-TW.md）要求修的事，每一項都有對應的測試：

1. **MF-1 單調性**：443e95a 遮得到的值，新版一定遮得到。審查者找到 101 個最小反例（舊版遮、新版漏），成因是新規則先把非
   token 鍵名的值換成 ``***``，舊規則的值因此停得比 443e95a 晚，吞掉後面的鍵名 lead 或解碼後才認得的前綴。這裡有：審查者的
   101 個加報告的 6 個反例、我們自己的差分 fuzz（舊規則的開頭＋憑證鍵名＋後面的錨點與秘密）找到的同一族反例、以及依
   「這一行有哪些舊規則會動」分三種規則的行為（有 token 鍵、有 ``/private_`` 前綴、兩者都沒有）。
2. **MF-2 冪等**：``redact(redact(x)) == redact(x)``，涵蓋審查者的 71 個反例、自己的 fuzz 找到的、各種語料；前置遮罩不可讓解碼後
   才認得的前綴露出來（值尾端的 ``%80``、``%EF%BF%BD`` 擋住後面的 ``\\private_``，``IDEMPOTENT_PREMASK_EXPOSES_PREFIX``）。
3. **N-1**：沒有舊規則會動的行，值裡的 ``%2F``、``%5C``、``%26`` 之後才出現編碼空白或引號也整段遮；有 token 鍵或前綴的行
   維持 443e95a 的行為（已知限制，測試釘住現況）。
4. **N-2**：截斷點不切開最後一個參數。
5. **N-4**：變異測試存活的規則各補斷言（編碼的鍵名字母、``_``／``-``、lead，數字結尾的鍵名，Kelvin／長 s 折疊，
   ``_has_candidate`` 的 ``&``／``;`` 條款，乾淨值的排除清單…）。
6. **N-5**：refresh_token、id_token、auth_token、session_token、app_key、consumer_secret、passphrase 也是憑證鍵名。
"""
from __future__ import annotations

import logging
import random
import re
import time
from urllib.parse import unquote

import pytest

from mcp_admin_core import log_redaction as lr
from test_log_redaction_query_credentials import (  # noqa: F401 — access_sinks／app_logger_sinks 是 pytest fixture
    ALL_NAME_VARIANTS,
    CREDENTIAL_WORDS,
    OLD,
    SECRET,
    SECRET_A,
    SECRET_B,
    _SEPARATORS,
    _WINDOWS,
    _access_line,
    _log_access,
    _name,
    _name_variants,
    _numbered,
    _query_text,
    _segment_text,
    _shown,
    _value,
    access_sinks,
    app_logger_sinks,
)

T = "VKPRSITNOVPKRTSN"  # 另一個合成值（SECRET 之外）
TAIL = "TAILTAIL1"


_SECRET_SPLIT = re.compile(r"[=&?;'\"/# ]")


def _fixpoint(text: str) -> str:
    for _ in range(8):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def _tail_windows(text: str, width: int = 5) -> list[str]:
    """審查者的最小反例都以秘密結尾（``…=0nl_F``、``…'tjAqL``、``…#mbsFq``）：整個輸入解碼到不動點，最後一個 ``= & ? ; ' " / #`` 或
    空白之後的字串的 5 字元窗口，去掉在前面的固定文字裡也出現的窗口（鍵名的字母）。"""
    decoded = _fixpoint(text)
    secret = _SECRET_SPLIT.split(decoded)[-1]
    fixed = decoded[:len(decoded) - len(secret)]
    windows = [secret[i:i + width] for i in range(max(1, len(secret) - width + 1))]
    return [window for window in windows if window not in fixed]


def _assert_monotone(text: str, windows: list[str], layers=("redact", "access")) -> None:
    """443e95a 遮掉的窗口，新版也遮掉；新版的結果再遮一次不變。"""
    for layer in layers:
        old, new = ((OLD.redact(text), lr.redact(text)) if layer == "redact"
                    else (_access_line(OLD, text), _access_line(lr, text)))
        lost = _shown(new, windows) - _shown(old, windows)
        assert not lost, (layer, text, old, new)
    once = lr.redact(text)
    assert lr.redact(once) == once, (text, once, lr.redact(once))




# ---------------------------------------------------------------------------
# 1. MF-1：單調性（443e95a 遮得到的，新版一定遮得到）
# ---------------------------------------------------------------------------

# 報告 MF-1 列的反例與 repro.py F1（443e95a 遮、c3443a5 漏）。
REPORT_EXAMPLES = [
    f"/x?token=A;key=%22?to%20ken={SECRET}",
    f"/x?token=A;key=%20?to%20ken={SECRET}",
    f"/x?token=?key=%20\\p%5Crivate_'{SECRET}'",
    f";token=?kEy=%20/pr%6%39vate_'{SECRET}",
    f"/api/logs/stream?token=abc%2Fdef;api_key=%20?to%20ken={SECRET}",
    f"/api/logs/stream?token=A/B;password=%27?t%26oken={SECRET}",
]


@pytest.mark.parametrize("text", **_numbered("report", REPORT_EXAMPLES))
def test_report_counterexamples_are_masked_like_443e95a(text):
    assert SECRET[:8] not in OLD.redact(text)  # 舊版遮
    out = lr.redact(text)
    assert SECRET[:8] not in out and SECRET[8:] not in out, out
    _assert_monotone(text, [SECRET[i:i + 5] for i in range(len(SECRET) - 4)])


def test_report_counterexamples_keep_the_readable_shape_of_443e95a():
    # 值以編碼過的引號開頭的非 token 鍵名在這一行沒有被前置遮罩吃掉，輸出與 443e95a 逐字相同（有 token 鍵：``?``／``;`` 的非 token
    # 鍵名值不整段乾淨，留給解碼後的規則）
    assert lr.redact(f"/x?token=A;key=%22?to%20ken={SECRET}") == OLD.redact(f"/x?token=A;key=%22?to%20ken={SECRET}")
    assert lr.redact(f"/x?token=A;key=%22?to%20ken={SECRET}") == '/x?token=***"?to ken=***'


MONOTONE_REVIEWER = [
    "&TO\u212aEN=;SIg=%20?token =0nl_F",
    ";token=?JWT=%20?token%26=6HnwU",
    "%3Btoken=;jWt=\"8?token%26=bGtX8",
    "?token=;pwd%3D%20?token%20=QGHDl",
    "?token=;sig=%22;t%26oken%3D7-yAC",
    ";token=?kEy=%20/pr%6%39vate_'tjAqL",
    "&%54OKEN=;%6beY=%20?tok%3Ben =0jABv",
    "?token=;pwd=%27;t oken=5%2530%52%64k",
    "?%74o\u212a%65%6E=;auth=%27?tok%20en=bnruR",
    "?ACCESS%2dTOKEN=?SIG=%20?tok en=lL_Yy",
    "&TOKE%4e=;SIG=%22;accesstoke n=%65iKzY",
    "&token=;PASSWD=%20%3Btok%3Be%26n=bwbwp",
    ";TOKE%4e=;p%77d%3D%20?t%20oken=Mm6%6Av",
    ";token=?auT%68=%20%%32Fprivate%5f\"2_prY",
    "&token=;AUT%48=%20;t%26o%3Bke%20n=MM%62GL",
    "?to%25256Ben=%25253fPwd%3D%27?tok en=S6DK_",
    "?access_to%256ben=?pwD=%22%3bto%20ken=pA26V",
    "%3BToK%45n=?API%2dKEY=%20%253btok%20en=Y88R2",
    "%3Ftoken%%33D;ApI%5FkeY=%27%3Btok en=%79ypCb",
    "?token=%253Fsecre%74=%20%253bt%3Boken =XPt3L",
    "&toK%2545N%3d?sig=%20?acc%26esstoke%3Bn=VZNHF",
    "&token=?PriV%41tEkey=%27?a%20ccesstoken=1j_l0",
    "?acceSS_%74o\u212aen=;ACCESSKEY=%22?t%26oken=GLeJL",
    ";TOKEN%3d?ACCESS%4BEY=%20?accesstoke%26n=VQLyL",
    ";toke%6E=;secretke%79=%20%3%42token =r%58%68WE",
    "?TOKEN=%3fapi%2Dkey=%20%3baccessto%20ken=bpI9I",
    "?acce\u017fstoken=?api%5fkey=\"j%2%46prIvate_/&xY2JV",
    "&TO%2525254BEN=?KEY=%20%%32Fpr%49vate_?&z4%49O1",
    ";TO%4BEN%3D%3b%4BEY=%20?ac%20ces%26stoken=gQDn-",
    "%26token=?SIGNATURE=%22%2f%70rivate%%35%46\"NvYO3",
    ";TOKEN=%3fsI%2567=%20;ac%26cesstok%20en%26=ORNbD",
    "%253baccess_token=;jwt=%27?accesst%26oken=%2dW5pB",
    "%3Faccessto%6Ben=?passwd=%22%2fprivate%5fe?&f4dLg",
    "&%61ccess%5ftoken=%3FPWD=%27?tok%20en=S%252550Nf-",
    ";to\u212ae%6e=%3Baccess%5fke%79=%27?acce sstoken=hYC2t",
    "?%74OKe%6E=;S%69%2547%3D%27?to%3Bk%26en=8%6F%2dpJ",
    "?Tok%65n=%3FP%52I%2556ATEK%45Y=%20?to%20ken=ivXgO",
    "?ac%63ess%255Ftoken=;jwt=%20?a%26ccesstoken=rtdzZ",
    "&TOKE%4e=?a%70i%5fkey=%27%25%32Fprivate%5F'%7501n5",
    "&token=;authorizatio%6E%3D%20?t%3Bo%3Bk%20en=PTLtA",
    "?ToKen=?XAMZSIGNATU%2552E=%22/privatE_Y?%26Bd%66ID",
    "?to%6Ben=;pa%73swd=%20%3fa%3Bcces stoken%3%441j2mZ",
    "?token%3D;jwt=%27%3Facces%20st%20ok%3Ben%3B=%69BSgB",
    "%25253BTOKEN=%3BSIG=%22?ac%26c%3Besstoken%20=ug%38Uh",
    "&ACCES%53_TOKEN=;api%5Fs%65cret=%20?to ken=8%6d%4BRn",
    "&acc%2565\u017fsto%6Ben=?%6bey%3D%20?access token=Hi%75Pz",
    "?ACCE%%353S%5FTOKEN%3d%3bpwd=%22?to%20ken%26=A%76ZSi",
    "?AcceSsTOkEn=?auth=%20?acce%26sstoke%3Bn=0%%343%6d2A",
    "&To%6be%6e=%3Fpa%73sw%256frd=%22?accesstoken%20=hSK2_",
    ";tO\u212a%65n=%3FSIGNATU%52E=%20\\pr%25C4%25b1vate_'vh%37ZU",
    "?ACCESSTOK%45%4%65=?prIvatekEY=%20?accessto ken=7p4uM",
    "?TOKEN=?XaMzSe%2543URitY%5FTOk%2565n=%27?toke n=YAW4a",
    "%253BtokEn=%253bApi%255FSeCre%54=%20?to%20k%3Ben=-bQyw",
    ";ACCE\u017fS%2DTOKEN=?apikey=%27?a%26c%20cesstoken%26=3eGFi",
    "%26aC%63eSsToK%45%4E=%3Fapik%2565y=%22?token =rYd%2532T",
    ";%25252574oken=?%73ecret=%20/p%2572iva%2574e_d?&%64KUeB",
    ";%54OKEN=%3BsEcreT%4bEY%3D%20%5Cprivat%65_/\ufffd?%26J8m%50A",
    ";%61cce%73%2573token=?APIKE%59%3D%20%2%46private_\"nzTCe",
    ";accesS%5ftO\u212aen=?xgoogsig%256Eat%75re=\":;token%26=rHyr1",
    "%3bACC%45\u017fSTOKEN=?p%61ssword=\"F%2%46priV%61T%65%5f\"4rOwb",
    "&accesstoken=%253fpassword=%22?accesst%20ok%20e%3Bn=Z8wJp",
    "&tO\u212aEn=%3bclie%6Et%5Fsecret=%27%3Fa%26ccessto%26ken=Hvf8a",
    ";TOK%2545N=%3F%61uthori%7Aation=%22%%32Fprivate_?&2%47H07",
    ";acces%2573%5ftoken=;SECrEt%4bEy=%E2%80%A8?token =Y%33HFD",
    "?toK%2545n=;pr%69vatek%2565y=%20;acce%20sstoken=%73GI%75D",
    "&AcceSS%255FtO\u212aEn=;accesskey%3D%22?acces%20st%20oken=658of",
    "&token=;signa%25252574ure=%27?a%20cc%20esstoken%20=%652xkt",
    ";accESs%5fToken=%253fsecretk%65y=\"5;t%26ok%20e%3Bn=J%79qim",
    "%26aCCEss_To%4b%45n=?P%25252577d=%20%3Baccesstok%26en=B6q5P",
    ";acces%73to%E2%84%aae%256e=?AUTH=%27%2%46%70ri%76aTe_'JksdE",
    "?%252561c%63ess%2dtoken=?PWD=%20%2%46p%2572iva%2574e_'Wj9B_",
    "?TO\u212aE%2525254E=;passwd=%20;ac%3Bcessto%3Bken%26=G%252570HvR",
    "&token=?x%2567oo%67%5fsigna%2574ure=%20%2FPrivate_s?%26Jy7m7",
    "?toke%25256e=;apI%2Dsecret=%20;acc%26es%26st%20oke%26n=mDUMp",
    "%2526token=?X%2DGOOGSIGNATURE=%27;a%20c%26cesstoken%3D%476MIX",
    "&TokeN%3%44?x%5Famz%73ign%61ture=\"F%3facce%26sstoken=VqJ%2532f",
    "&ACCE\u017fS%5FTOK%45N%3D;SECRETKEY=%20?a%3Bc%3Bcesst oken%253DSeCgE",
    ";token=;x%255Fam%7a%5fsigna%2574ure='_;accesstok%26en=QU%57%52S",
    "?ACC%45SS%5FTOKEN=?pRi%56aTeKE%59=%22%3Btoke%20n%3B=%25430%42jC",
    "&ACCESS%5fTO%4BEN=%3bAUTHORIZATION=%20;ac%3Bcesstoke%26n=doH%34P",
    "&acces%73%74oken=%253fpRIvaTe%2dkey=%20?acces%20stoken=uM%61%72I",
    ";access_token=;PR%49VATE%5FKEY=%20?ac%20cessto%3Bken=MK%%33%35tD",
    "&%54OK%65N=%253F%78aMZSIgn%41tuRE=%22?acces sto%3Bk%3Ben%3B=BH2UV",
    ";%41%43CESSTOK%45N=?AcCEss%5FkeY=%20%%32F%2570rIva%74e%5f?;e%6fPUQ",
    ";TOKEN=;%78%2Da%6dzsecur%69%2574ytoken%3D%27?a%20ccesstoken=FeP%50v",
    "?ac%2563%65ss%5ftoke%25256e=?pRIvaTe%5fKEy=%20?t%26o%26ke%26n=dUeBF",
    "%26token=%3Fxamz%2ds%65curity%255Ftoke%256e=%27%%32FpRi%56ate_#v-4-u",
    ";t%6F%25E2%2584%25AAe%%36%65%3D;x%5fapi%5F%6b%65y=%20;tok%26en=-Bxtf",
    ";aCcess%5FtokEn=;CLI%2545%4ETSECRET%3D%27;acce%3Bsstok%26en%3B=q%67Jof",
    "?t%%36%46ken=?clien%2574secre%2574=%20;accessto%26k%3Be%20n%26=%74oMqh",
    "?token=?xamz%5fsecuritytok%65n=%27?ac%26ce%20ssto%20ke%20n%20=Jj%39b%37",
    "%3%62acc%65ss_%25252574oken=;SeCRe%54%2DkeY=%20;accesst%20oken%253DKrsQ9",
    "&t%6fken=%3bx%5F%61mz%2Dsec%75rity%5Ftoken=%22%3Bt%26ok%3Be%20n%3B=_wEHz",
    ";access%255Ftoke%6E=?CLIENT%2dSECRE%54=\"B?a%3Bc%3Bcess%20token=%63pE%2552X",
    "?acc%2565ss_t%256fk%252565n=;%78go%6Fgsignatur%2565=%22?accesstoken =XoU_S",
    "&acc%20es%20stoke%26n=?client%25252573ec%72et=%20/%7%30riv%252561Te_'%70Hzjn",
    "?a%3Bccesstoken=?X%2dgOog%5FsI%67n%41turE=%20?a%3Bcc%3Be%20sst%20oken=%635Pw6",
    ";token=?x%5Famz%5Fsecuri%2574%79to%6be%256E=%20%253Facc%20esstoke%3Bn%26=lPm7T",
    "?accesS%5FtO%6Ben=;x%5famz%2Dsecuritytoke%256e=%22%3%66accesstoke n%253DA%5frf-",
    ";acces%73token%3D%253Fx%5Famz%25252573ecuritytoken%253D%20\\p%7%32ivat%2565_#mbsFq",
    "%253BACCESS%255F%252554%2525254fKE%4E=%25253bx%2Dapi%25254Bey%3D%22%253Fac%3Bcesstoke%26n=Kw0lA",
]


@pytest.mark.parametrize("text", **_numbered("reviewer", MONOTONE_REVIEWER))
def test_reviewers_101_minimal_counterexamples_are_masked_like_443e95a(text):
    """審查者縮小出的 101 個不重複最小反例（443e95a 遮、c3443a5 漏）：非 token 鍵名的值先被換成 ``***``，前一個「值不乾淨」的
    token 的值因此一路吞過後面鍵名字母之間夾空白的 lead、或解碼後才認得的 ``/private_`` 前綴。"""
    windows = _tail_windows(text)
    assert not _shown(OLD.redact(text), windows), "舊版應該遮得到"
    _assert_monotone(text, windows)


# 自己的差分 fuzz（下面的產生器，對 c3443a5 跑 60 萬行）找到的同一族反例，縮小後挑 86 個：``(層, 輸入, 窗口)``。
# 除了 token 值吞過 lead 之外，還有兩個前綴之間夾著遮掉的值（前面的區段一路吞過去、吃掉後面前綴的起點）、解碼後的值規則吃掉
# 字面引號或 ``?``（前綴的區段規則與裸 query 要靠它們判斷在哪裡停）。
MONOTONE_FUZZ_FOUND = [
    ("redact", ";token=?key=%20? token=i11jy", "i11jy"),
    ("redact", "?Token=?PWd=%22; token=ioMU8", "ioMU8"),
    ("redact", "&token=?auth=%22? token=F1wYN", "F1wYN"),
    ("redact", "&to%6Ben=?jwt=%20?toke n=-6Cnk", "-6Cnk"),
    ("redact", ";token=;SIG=%20%3Btok en=0OZAQ", "0OZAQ"),
    ("redact", "?token=;PWD=%20?to%20ken=lD8c0", "lD8c0"),
    ("access", "?token=?sig=%20%3Btoke n=e2nq5", "e2nq5"),
    ("redact", ";token=?apikey=%27; token=B3rCF", "B3rCF"),
    ("redact", "?%74oken=?auth=%22? token=DPigJ", "DPigJ"),
    ("access", "%3fTOkEN=?sig=%27;tok en%3D62Ds4", "62Ds4"),
    ("redact", "&tokE%4e=?apikey=\"b? token=sJyEU", "sJyEU"),
    ("redact", "&token=?%70wD=%22? token=%6BP0bm", "kP0bm"),
    ("access", ";TO%4BEN=;pwd=%22; token%3DFR97e", "FR97e"),
    ("redact", ";TOKEN=?autH=%20? tok%25en=Ja76U", "Ja76U"),
    ("redact", "?%74OKen=;kEY=%22; token=Ho%76Ap", "HovAp"),
    ("redact", "?TOKEN=?jwt=%22%3F to%6ben=37wu2", "37wu2"),
    ("access", "?token=%253Fpwd=%20; token=jk166", "jk166"),
    ("access", "?token=%3Bkey=%20?t%20oken=xMPVY", "xMPVY"),
    ("redact", "?token=;pwd=%22;toke%20n=%4CxpEU", "LxpEU"),
    ("redact", "?token=?key=%27?tok%20en%3DKeocC", "KeocC"),
    ("redact", "%26token=;AUTH=%20? %74oken=8D9Wv", "8D9Wv"),
    ("access", "%26token=;KEY=%20? t%3Boken=6qM7D", "6qM7D"),
    ("redact", "%3Fto%6ben=;pwd='b?%20token=AWvLW", "AWvLW"),
    ("redact", "&To\u212aeN=%3Bpwd=\"1?toke%20n=H%43OaO", "HCOaO"),
    ("redact", ";TOKEN=;AUTH=%22?%20token=%45YDRh", "EYDRh"),
    ("redact", ";TOKEN=;PASSWD=%20?to%20ken=7R9d5", "7R9d5"),
    ("redact", "?TOKEN=%3Fauth=%27%3B token=aArZo", "aArZo"),
    ("redact", "?TOkeN=%3BAUT%48=%20? token=heZGK", "heZGK"),
    ("redact", "?t%6Fken=?SIG='=%3Ft%26oken=L9D0Z", "L9D0Z"),
    ("redact", "%3FTOKEN=;pwd=%22?tok en=8H%79%4fn", "8HyOn"),
    ("redact", "&token=%3Bjw%74=%20?%20token=3Tz7y", "3Tz7y"),
    ("redact", "&token=?%53E%43RET='x; token=gW4m-", "gW4m-"),
    ("redact", "&token=?API%2DKEY=%22; token=taN_C", "taN_C"),
    ("redact", "&token=?PWD=%20?%20token=%2549Vyca", "IVyca"),
    ("redact", ";TOKEN%3d;pw%64=%27?toke n=6c_%56O", "6c_VO"),
    ("redact", ";TOke%256E=?key=%20;to%26ken=ej5v6", "ej5v6"),
    ("redact", ";toke%6e=%3BAPikEy='_? token=LzQa0", "LzQa0"),
    ("redact", "?t%4fken%3D;jwt=%c2%a0?t oken=7s8m5", "7s8m5"),
    ("redact", "&%74oke%6E=;PWD=%C2%A0?%20token=ZBp2g", "ZBp2g"),
    ("redact", "%3btoKEn%3D?AUTH=%27;t%26ok%25en=EYq9u", "EYq9u"),
    ("redact", "&%41CCESS%5FTOKEN=;jwt=%20; token=R7f53", "R7f53"),
    ("redact", "&TOKEN=?P%77D=%27%3B%20token=7t%72%2565f", "7tref"),
    ("redact", "%3Ftoken=;siGnaturE=%27;to%26ken%20=tK7rA", "tK7rA"),
    ("redact", "?tOk%25252565n=;apikey=%20?%20token=BTHKZ", "BTHKZ"),
    ("redact", "&token=?%254AWT=%22;a%26ccesstok%65n=iA8zT", "iA8zT"),
    ("redact", "&token=%253Bj%57T=%20?%20to%2Eken=4%50uM%35", "4PuM5"),
    ("redact", "?TOKEn=%3Faut%2568=%E2%80%A8;%20token=ICeK3", "ICeK3"),
    ("redact", "&TOKEN=;api%5Fsecret=%C2%A0;%20token=HI5C%6E", "HI5Cn"),
    ("redact", "?to%25256ben=?signature=%22?%20token%3DheIIh", "heIIh"),
    ("redact", "&token=%3Bsecret%2d%6Bey=%22?token%20=%45y_2_", "Ey_2_"),
    ("redact", "?tOKen%3D;secret%3D%22%3Ftok%20e%256e=C%6FO9q", "CoO9q"),
    ("redact", "&token=?x%5famz%5Fsignature=%22;%20token=w1LlA", "w1LlA"),
    ("redact", "?tok%65n=%3Fapi%255F%6be%79=%20; tok%65n=zr76X", "zr76X"),
    ("redact", ";TO%4BEN%%33D%3Bau%74h=%E3%80%80%3Ftoken =mopL2", "mopL2"),
    ("redact", "%253BTOKEN=;%61pisEc%72ET=%27; token=%5F%2546Ltb", "_FLtb"),
    ("redact", "&to%6ben=%3F%61pikey=%C2%A0%3F token=05G%48%5%36", "05GHV"),
    ("redact", "?aCCESsToKen=%3Fkey=%E3%80%80? %2574oken=e%5fy86", "e_y86"),
    ("redact", "&TO%4bEN=%3BAPI%2dSEC%2552ET=%20?%20token=eRv%42B", "eRvBB"),
    ("redact", "?T%4FKEN=%3FsEcRet%2Dkey=%E2%80%8A%3B token=ZlaXp", "ZlaXp"),
    ("redact", "&TO%4B%2545N=?access%5Fkey=%20%3B%2Etoke%20n=9-J7j", "9-J7j"),
    ("redact", "?T%4fKEN=?jw%252574=%E2%80%A8%3B to%25256ben=zjL7H", "zjL7H"),
    ("redact", "&token=?API%5F%53%45CR%45T=%E2%80%8a?tok%26en=buAsN", "buAsN"),
    ("redact", "&%54%%36%46KEn=%253Bsig=%20%3Bacce ssto%256ben=1BPbH", "1BPbH"),
    ("redact", "?%252554OKE%254e=?x%2dapi%2dkey=%22%3Ftoke n%3DOJ9rY", "OJ9rY"),
    ("redact", "%3FTOKEN=?secretke%79=%20%3B%20t%20o%256B%65n=aJ0t%6c", "aJ0tl"),
    ("redact", "%3Btoken=%253Fjwt=%c2%a0%3Faccesstoke%26n%2E=%56a%4cE0", "VaLE0"),
    ("redact", ";token=%253BxamzsE%63uriTyTOken=%22?acces stoken=xPnNm", "xPnNm"),
    ("redact", "&accESSTOkE%4E=?PwD%3D\"b%3F%20acce%73%26sto%2Dken=QNld0", "QNld0"),
    ("redact", "%253b%74oken=?SIGNATURE=%27%5Cp%72iva%2574e%%35%46\"O37xG", "O37xG"),
    ("redact", ";ACCESSTOKEN%3%44%3FAUTHORIZATION=%20?%20token=-N%4%43Ux", "-NLUx"),
    ("redact", "&%2561cCEsS%2DtokEN=?password=%20? accessto%25ken=po4%68T", "po4hT"),
    ("redact", "%26token=%253Fx%2da%2570%69%5fkey=%20;a%20ccesstoken=6fGHI", "6fGHI"),
    ("redact", "?accesstoken=?SECRET%5fKEY=%27?%20t%26oken%3d%70%254bl%43s", "pKlCs"),
    ("redact", ";acces\u017ftoken=?authorization=%20%3F%2Eacc%65%20sstoken=8UcjI", "8UcjI"),
    ("redact", "?ACCESS_TOK%45N=%253Bau%74%2568o%72izat%69on='D; token=73TFo", "73TFo"),
    ("redact", "?token=?x%5famz%5Fsecuritytok%65%6E=%20%3Bto%2Eken =rh3b%4%64", "rh3bM"),
    ("redact", "%3bTOKEN%3d;X%47OOG%255FSI%25252547NATURE=%c2%a0;to%20ken=5c00y", "5c00y"),
    ("redact", "%253baccess%5Ft%6Fken=?%61Pi%5fKEY=%27;%2Eaccesstoken%26=wcK%6FX", "wcKoX"),
    ("redact", "%26token=?X%5Fgoog%2dsIgN%41T%2575R%65=%E2%80%8a;%20token=%753c0Z", "u3c0Z"),
    ("redact", "%26token=%3FX%2Dam%5Asecu%52i%2574YtokeN=%22?ac%2Dcesstoken =OKcwp", "OKcwp"),
    ("redact", "&tok%65n%3D?%2570RiVate%5F%4beY=\"B/pr%2569%%376a%74e%5F%27%254AV6FX", "JV6FX"),
    ("redact", "&%41CCESS%5fTOKEN%3D;access%2Dk%65y=%E2%80%A8%3Baccesstoke%26n=bwzKw", "bwzKw"),
    ("redact", "?Tok%65n=?x%41mz%2Dsig%256EatU%72E='D%3F%20accesstok%25e2En=F9Q%2538c", "F9Q8c"),
    ("redact", "%3Ba%2563%63e%73s%255Fto\u212aen=%253Fpriv%25252561t%65key=%20?toke%26n=F22iU", "F22iU"),
    ("redact", "%26%2574oken%%33D%3FSEC%252552E%54%3D%20%3B%25252561%20cc%65ss%74oken=YMxC2", "YMxC2"),
    ("redact", "%26aCc%25252565s%73_toKE%254e=%3Bsec%2572etk%2565y=%22;accesstok en=b7%4d%39%53", "b7M9S"),
]


@pytest.mark.parametrize(("layer", "text", "windows"), **_numbered("found", MONOTONE_FUZZ_FOUND))
def test_fuzz_found_counterexamples_are_masked_like_443e95a(layer, text, windows):
    windows = windows.split("|")
    old, new = ((OLD.redact(text), lr.redact(text)) if layer == "redact"
                else (_access_line(OLD, text), _access_line(lr, text)))
    assert _shown(new, windows) <= _shown(old, windows), (layer, text, old, new)
    once = lr.redact(text)
    assert lr.redact(once) == once


# --- 差分 fuzz 的產生器：舊規則的開頭（值會一路吞下去）＋憑證鍵名（值以編碼分隔字元開頭）＋後面的錨點與秘密 ---------------------

_FAMILY_STOPS = ["%20", "%22", "%27", '"', "'", "%C2%A0", "%E3%80%80", "%2520", "%20%20", "%22%20", "%E2%80%A8"]
_FAMILY_CLEAN = ["", "a", "ab", "abc", "-", "_", "=", "%2B", "%41"]
_FAMILY_UNCLEAN = ["", "", "/", "?", ";", "\\", "*", "%2F", "%3F", "%3B", "%5C", "%26", "&", "%", "/p", "\\p", "�"]
_FAMILY_OPENER_VALUES = ["", "A", "abc", "A;", "A?", "A/B", "?", ";", "/", "\\", "a%20b", "A;x=", "A?x=", "%5C", "%3F"]
# 只有解碼後才認得的前綴（十六進位數字本身也編碼，或反斜線前面再編碼一層）
_FAMILY_PREFIXES = ["/p%5Crivate_", "/pr%6%39vate_", "%5Cp%5Crivate_", "%2Fp%5Crivate_", "\\pr%6%39vate_", "/%2570rivate_"]
# 鍵名字母之間夾空白或標點的 token 鍵名（舊規則的寬鬆比對）
_FAMILY_LOOSE = ["to ken", "t.o.k.e.n", "to%20ken", "t%26oken", "tok%3Ben", "ac cesstoken", "a%20ccess_token", "acc%26esstoken"]
_FAMILY_LEADS = ["?", ";", "&", "?", ";", "%3F", "%3B", "%26", "%253F", "%253B"]


def _family_text(r: random.Random) -> tuple[str, list[str]]:
    secrets: list[str] = []
    parts = [r.choice(["", "GET ", "/x", "/api/logs/stream", "msg: ", "/private_x/mcp"])]
    for _ in range(r.choice([1, 1, 2])):  # 舊規則的開頭：值會一路吞下去
        kind = r.choice(["token", "token", "token", "loose", "prefix"])
        if kind == "token":
            parts += [r.choice(_FAMILY_LEADS), _name(r), "=", r.choice(_FAMILY_OPENER_VALUES)]
        elif kind == "loose":
            parts += [r.choice("?&;"), r.choice(_FAMILY_LOOSE), "=", r.choice(_FAMILY_OPENER_VALUES)]
        else:
            parts += [r.choice(_FAMILY_PREFIXES), r.choice(["x", "ab", "", "x&", "x;"])]
    for _ in range(r.choice([1, 1, 1, 2, 3])):  # 憑證鍵名，值以編碼分隔字元開頭
        parts += [r.choice(_FAMILY_LEADS), _name(r), "=",
                  "".join(r.choice(_FAMILY_STOPS if r.random() < 0.8 else _FAMILY_CLEAN) + r.choice(_FAMILY_CLEAN)
                          for _ in range(r.choice([1, 1, 2, 3]))), r.choice(_FAMILY_UNCLEAN)]
        if r.random() < 0.3:
            parts.append(r.choice(_FAMILY_CLEAN) + r.choice(_FAMILY_UNCLEAN))
    for _ in range(r.choice([1, 1, 2])):  # 後面的錨點與只有那條舊規則遮得到的秘密
        key = "B" if "B" not in secrets else "A"
        secret = SECRET_B if key == "B" else SECRET_A
        if r.random() < 0.6:
            parts += [r.choice("?&;"), r.choice(_FAMILY_LOOSE), "=", _value(r, secret)]
        else:
            parts += [r.choice(_FAMILY_PREFIXES), r.choice(["", "'", '"', "?"]), _value(r, secret)]
        secrets.append(key)
    parts.append(r.choice(["", "&tail=1", " done"]))
    return "".join(parts), secrets


def _pathed_text(r: random.Random) -> tuple[str, list[str]]:
    """``/private_<token>/mcp?…``：區段先在原本的拼法上遮，裸 query 的連鎖（``?***`` 加空白接成鍵名 lead）。"""
    secrets: list[str] = []
    parts = [r.choice(["", "GET ", '127.0.0.1:5555 - "GET ']), r.choice(["/private_", "/PRIVATE_", "/pr%69vate_", "%2Fprivate_"]),
             _value(r, SECRET_A), r.choice(["/mcp", "/messages/", "/sse", ""]), r.choice(["?", "?", "%3F", "?&"])]
    secrets.append("A")
    for index in range(r.choice([1, 2, 3, 4, 5])):
        if index:
            parts.append(r.choice(["&", "&", ";", "%26", "?"]))
        kind = r.choice(["bare", "cred", "cred", "cred", "token", "token", "loose", "plain"])
        if kind == "bare":
            parts.append(r.choice(["bare", "b", "debug", "a%20b", "", "z1"]))
        elif kind == "plain":
            parts.append(r.choice(["limit", "page", "session_id"]) + "=" + r.choice(["1", "abc", "", "x%20y"]))
        elif kind == "cred":
            parts += [_name(r), "=", "".join(r.choice(_FAMILY_STOPS if r.random() < 0.6 else _FAMILY_CLEAN)
                                              for _ in range(r.choice([1, 2, 3]))), r.choice(_FAMILY_UNCLEAN)]
        elif "B" not in secrets:
            parts += ([_name(r), "=", _value(r, SECRET_B)] if kind == "token"
                      else [r.choice(["", "%20", " "]), r.choice(_FAMILY_LOOSE), "=", _value(r, SECRET_B)])
            secrets.append("B")
    parts.append(r.choice(["", "", " HTTP/1.1", "#frag", '"', " "]))
    return "".join(parts), secrets


@pytest.mark.parametrize(("family", "make", "seed"), [("opener+credential+anchor", _family_text, 20261114),
                                                       ("/private_ url", _pathed_text, 20261115)])
def test_family_differential_fuzz_never_shows_what_443e95a_masked(family, make, seed):
    """redact() 與 access log 兩層：443e95a 遮掉的秘密窗口新版一定遮掉；結果冪等。「opener+credential+anchor」專找「值先被換成
    ``***`` 之後舊規則停得更晚」這一族：對 c3443a5 每 12,000 行約 70 筆違規（本機實測 71）；「/private_ url」專找區段與裸 query 的
    連鎖，對 c3443a5 12,000 行 0 筆（密集產生器每 4 萬行約 4 筆，見 ``MONOTONE_FUZZ_FOUND``），這裡主要守冪等與 access 層。"""
    r = random.Random(seed)
    checked = access_checked = 0
    for _ in range(12_000):
        text, secrets = make(r)
        windows = [w for key in secrets for w in _WINDOWS[key]]
        present = _shown(text, windows)
        if not present:
            continue
        checked += 1
        old, new = OLD.redact(text), lr.redact(text)
        lost = (_shown(new, windows) & present) - _shown(old, windows)
        assert not lost, (family, text, old, new)
        assert lr.redact(new) == new, (family, text, new, lr.redact(new))
        if text.isascii() and text.isprintable() and " " not in text and text.startswith("/"):
            access_checked += 1
            old_line, new_line = _access_line(OLD, text), _access_line(lr, text)
            lost = (_shown(new_line, windows) & _shown(text, windows)) - _shown(old_line, windows)
            assert not lost, (family, text, old_line, new_line)
    assert checked > 9_000, checked


# --- 三種行（見 log_redaction 第 1 點）的行為 ------------------------------------------------------------------------

def test_line_with_a_token_key_still_masks_values_of_ampersand_led_credentials():
    # lead 是 ``&``：舊的 token 值一定在 ``&`` 停下，值以編碼空白開頭、後面又有 ``/`` 的密碼照樣整段遮
    assert lr.redact("/x?token=abc&password=%20my%20pass/word&limit=5") == "/x?token=***&password=***&limit=5"
    assert lr.redact(f"/api/logs/stream?token={SECRET}&api_key=%20{SECRET}/{T}") == "/api/logs/stream?token=***&api_key=***"


@pytest.mark.parametrize("lead", ["?", ";"])
def test_line_with_a_token_key_leaves_unclean_values_of_question_and_semicolon_led_credentials_to_443e95a(lead):
    """已知限制（與 443e95a 相同，不是退步）：舊的 token 值會一路吞過 ``?``、``;``，這兩種 lead 的非 token 鍵名值只在整段乾淨、
    一路到字面分隔字元時才在原本的拼法上遮；``%20`` 之後又有 ``/`` 的值留給解碼後的規則（遮到解碼出來的空白為止）。"""
    text = f"/x?token=abc{lead}password=%20my%20pass/word&limit=5"
    assert lr.redact(text) == OLD.redact(text) == "/x?token=*** my pass/word&limit=5"
    clean = f"/x?token=abc{lead}password=%20mypass&limit=5"  # 整段乾淨：前置遮罩遮掉，token 的值一路吞到 ``&``
    assert lr.redact(clean) == "/x?token=***&limit=5" and "mypass" not in lr.redact(clean)


def test_line_with_a_private_prefix_keeps_the_clean_prefix_rule():
    """已知限制：有 ``/private_`` 前綴的行不放寬（裸 query、前綴區段的判斷要照 443e95a），值遮「乾淨的開頭」，``%2F`` 之後才出現
    的編碼空白留給解碼後的規則。"""
    assert lr.redact(f"/private_x/mcp?api_key=%20{SECRET}") == "/private_***/mcp?api_key=***"
    assert lr.redact(f"/private_x/mcp?api_key=%20{SECRET}%2F{T}%20{TAIL}") == "/private_***/mcp?api_key=*** " + TAIL
    assert lr.redact("/private_x/mcp?api_key=%20a%2Fb") == "/private_***/mcp?api_key=***"


def test_two_decoded_only_prefixes_with_a_masked_value_between_them_stay_masked():
    """第一個前綴的區段會一路吞過前綴後面遮掉的值、吃掉第二個前綴的起點（兩個前綴都是解碼後才認得的）：這種行不做前置遮罩。"""
    text = f"%2Fpr%6%39vate_?jwt=%27B\\pr%%369vate_'{SECRET}"
    old, new = OLD.redact(text), lr.redact(text)
    assert SECRET[:8] not in old and SECRET[:8] not in new, (old, new)
    only_one = f"/pr%6%39vate_?token=%20abc {SECRET[:6]}"  # 只有一個前綴：照樣前置遮罩，冪等
    assert lr.redact(lr.redact(only_one)) == lr.redact(only_one)


# ---------------------------------------------------------------------------
# 2. MF-2：冪等（redact(redact(x)) == redact(x)）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "shown"), **_numbered("quoted", [
    # 審查者的輸入：值以字面引號開頭，443e95a 認不出這個 token；前置遮罩遮成 token=*** 之後才符合「有 = 的參數加上裸參數」
    (f"/private_0b5e1d9c4c2f4c8e/mcp?access_token='{SECRET}&debug", "/private_***/mcp?***"),
    ('/private_X?token="v&bare', "/private_***?***"),
    ("/private_e?AccessToken='0&m", "/private_***?***"),
    ('/private_1?token="i&k', "/private_***?***"),
    # 同形狀換成新鍵名與 ``;``
    (f"/private_x?api_key='{SECRET}&bare", "/private_***?***"),
    (f"/private_x/mcp?password=\"{SECRET};bare", "/private_***/mcp?***"),
]))
def test_quoted_credential_before_a_bare_param_in_a_private_url_is_masked_in_one_pass(text, shown):
    assert lr.redact(text) == shown
    assert lr.redact(shown) == shown
    assert SECRET not in lr.redact(text)


@pytest.mark.parametrize("text, shown", [
    ("/private_e?to ken=V", "/private_***?*** ken=***"),
    ("/PRIVATE_Q?e&toke.n=V", "/PRIVATE_***?***"),
    (f"/private_x/mcp?to ken={SECRET}", "/private_***/mcp?*** ken=***"),
])
def test_bare_query_is_judged_again_after_the_key_rules_masked_a_value(text, shown):
    """``/private_`` 網址的 query 裡有 token 鍵（鍵名字母之間夾空白、點，只有鍵名規則認得）：鍵名規則遮了值之後，前面的裸參數才成了
    「沒有 = 的參數」，裸 query 規則要在同一次遮罩再判斷一次，否則第二次遮罩才把整段 query 遮成 ``?***``（不冪等）。``keys`` 為真時
    不論這一行有沒有編碼、有沒有前置遮罩都要判斷（變異「拿掉 keys」原本只被差分 fuzz 殺死）。"""
    assert lr.redact(text) == shown
    assert lr.redact(shown) == shown
    assert SECRET not in lr.redact(text)


def test_bare_query_is_judged_again_after_a_value_rule_masked_something():
    """``or masked_values``（``_redact_bounded``）：``jwt=*`` 的字面 ``*`` 不算乾淨，遮成 ``jwt=***`` 的是解碼後的值規則；
    之後裸參數 ``debug`` 要在同一次遮罩就整段遮掉（變異測試：拿掉這個條件，30 萬行只有這一類輸出不同）。"""
    text = "/PRIVATE_9Baw-HrPPqi3k4j%45hLYoZ/mcp?;jwt=*&debug"
    assert lr.redact(text) == "/PRIVATE_***/mcp?***"
    assert lr.redact("/PRIVATE_***/mcp?***") == "/PRIVATE_***/mcp?***"


# 審查者的 71 個不冪等輸入（c3443a5：第一次與第二次不同；443e95a 冪等）。成因都是 MF-2 的那一條：前置遮罩遮了東西之後，
# 裸 query 沒有在同一次判斷。
IDEMPOTENT_REVIEWER = [
    "[mcp-server] /private_sk-35ZMhu2dbzzNPTIHTUm3z4eS0gfCmY_C/mcp?token='sk-QiySobCewAlaNhdAM0HhbUiB%22&amp;limit=50",
    "url=/private_sk-RpW7_ixFKfiUcXB8Ce1C%60Nofd/sse?token='e172ae23c39a52725b2532d77e0e5e4d%60&amp;key_name=50;name=&q=",
    "[mcp-server] /private_sk-ThiLjySrjEst13Alfxj-Pd02DkVpFwEl/sse?token=\"L8i7OkeIedVbkoaeTfTVYI%29&amp;oauth_state=true\"",
    "/private_jxL8OB17A_JK8sYFqQrCs2c9ApUiqShD/mcp?keys=1&TOKEN='5R7nIlOfs7SPzp+xxHOuzvwr==&amp;session_token=true&q=abc done",
    "GET /private_sk-kQyop5vhg3Rd_aV-22zOaVjsA8vuIKpVeYOGOUQvPv0oGYIE/sse?token='K_GsDRSAPnj7rucV3MttDL&amp;lang=a+b HTTP/1.1\" 200",
    "/private_sk-tH13pHU4jS54z-WgavaudR30/messages/?token=\"Fjkvfrauecwc1Bucbhlqv8zGndUKrU5p2d2p9LTckZi&amp;key_name=abc extra words",
    "url=/private_%22%20sk-G-hoasatCNF-tAkMyUU6eZ5F%60/messages/?access_token=\"d82e8b946cf9b6db9e2a463a0fa3245f%60&amp;session_id=&keys=",
    "url=/private_x59x1zHUN7A8QRsy%2BQ38aVUTb6g9oHvdbbUWuwiK%3D%3D/mcp?access_token='sk-k8e5jFwSKaDp_CzNs9FJ_J73GEZxbhUM&amp;state=abc;author=50",
    "/private_%EF%BB%BFlC4wdcLwdrInoz3ANvQF1bf_H8uVM6L1%3E/messages/?token=\"T1qPXT7xYqcJvpcjP-dSIHiZwQs0IqQ2cBUwPFiujoU&amp;limit=true&amp;regex=1",
    "url=/private_0d8237f7db1cba7f3628a740c7b735d3c758d017c4261fb61db39918206212c0/mcp?author=a+b&access_token='g3zqaoTPW6sG7CObuzQKTI%5B&amp;order=1",
    "url=/private_sk-FYpQy6Ht3dNZAas5W1GKnijlP23Hz14r/sse?TOKEN='V0l9o4pHzKn0kZ4KEPyLnSdfrqcidWLo%22&amp;signature=6e0e3a4a89b970fa029561903db8f68c#frag",
    "/private_%3C15bef3649247a944a7d6d4545fea73e0fbe017da7bb93120/sse?TOKEN=\"eyJDkIDzwUnKch.eyJveLSZ8wVndN.KHWYUqhTFmkjXGw5JnMWv&amp;q=true&amp;sort_key=true",
    "GET /private_e02bc7a2a41dc6811d4428649404f7a2d59371eccd7c76ad/sse?token=\"EvafLLxE-6JqsxIEYo9F1I&amp;API_KEY=3bf88240b6f9dc5db98261c89ea54961b5d5cf0735c2e343",
    "url=/private_c916533e91f94fc1e40505f85ceac294/sse?access_token='7AgdKxmKYMYEYV1s9OJMFdowneNkjH0sP4gXVwrgeat%5D&amp;api_secret= 85a3a9b2551d8fd58f2546af868eb055 ",
    "127.0.0.1:1 - \"GET /private_+jf2GHiqX7s4qzm24dktSnDdzOdEYX_5B/mcp?access_token='sk-jsKTXtRqhzhbwiAii1U4zA-7&amp;auth=F_KlO4YKlyhz4e87Qkl7jEnn1r5qjRx744OH4QrgVyk\"",
    "GET /private_eyJ4j4HIaPXMUZszhH.eyJ0YHtEL3hvTyiPM1DOhEffH54Ci-.ihQFtUvn76ZPNgfnPhoCxF3xT5Vh/sse?Access_Token='79c50252d2a071f629fea06c78aaf2c1&amp;author=50#frag",
    "/private_%E3%80%800Ah1QQD2O0pe40TODgLXaozl=/sse?offset=a+b&Access_Token='eyJdTKPH1DsaHbA.eyJpaZEc4g5LsCEAgn98NC.KLOpXYc6DG53wETvvPmIK-p-vh1f&key_id=50&amp;author=1",
    "proxy failed: /private_c38731e5810cf3c1398de919620074f8bc91786ac7c76d22a59f27002ef665de/sse?token='2df72b31a3b514a637c5c7d0aadc5821%27&amp;keyword=true extra words",
    "GET /private_Udp23tuxxAK6HFmJYVKsqyFsAtapwwOxeBLNsTdw%3D/mcp?TOKEN=\"ee74d9e4af9f4fbfcd9bca127d0e58cecfa761f8ba6938f9&name=&amp;access_key=ad22868205eb0cf8ba2bfbf82d1f130d",
    "/private_%22Izleo-FtHNtBdwyxRqq2RwpXJM3LFiKZ/mcp?token='sk-Y2g8ugLqkeNj-6cUvrb6XpxZzStWUVzGHm7BXDaTBjIsxVFK&oauth_state=1&amp;apikey=%22sk-ubUcHnr3arH4YZTUITt7hXYn extra words",
    "[mcp-server] /private_-RgrwYzawBFVc320w8j8jFEyUCsgsDKi/messages/?token=\"707c1d36b0e16665753b96f0f0798c1c916e21ddc5e26d4f3e27f3608638b4dc&amp;author= HTTP/1.1\" 401 Unauthorized",
    "url=/private_%20f2ee22a70511267ccd4f24224265fb58c2ffb1218b9a2a46%5D/sse?Access_Token=\"c82bf26faf341a598debd16f3db65f85&amp;X-Goog-Signature=sk-T8w4AYst5OsNNMoHVHH6iia4lrfiivyE",
    "proxy failed: /private_2725a5ee3a652b39edd7bf5ba326b2784d4d9ac2cf9bf8e1/messages/?access_token=\"yXOBgkpCwwp5z78mNet24fAE==&amp;key_name=true?tail=true&public_key=1 HTTP/1.1\" 200",
    "/private_91ff2a7bd48db54dc4ebd6f99a2592fc0a230ded23609b31/sse?Access_Token=\"sk-c6YPrPi-Re7e-I1nTCSt5p9g+&amp;key_id=50;API_KEY=isZNGXzXTpfmAl6bYTdiAOxS662DiIHDy5D8uGJ8nNn&limit=1",
    "127.0.0.1:1 - \"GET /private_%29sk-5v1zk_mDAJA2A44QPaKObOXasJ6_efaKxuvIm-uV1UdFLVjf/messages/?access_token=\"e61890dadc3fbb1821571deca32e8220%E2%80%8B&amp;sort_key=abc HTTP/1.1\" 200",
    "GET /private_YF31jE7w8gGH6vOHf4JBDIdW=/sse?token=\"sO1sOU-QBW4AAFJ0Lp8FEpv88GPVisCx&private_key=sk-w4OJCNmScatxML9XAeFtsxanc1BItqGd&amp;password=cz7QjPgFeosMFSRIztyCTC&key_name=1#frag",
    "/private_eyJVvmMpmORLXZhMYfkZSurdMmB-RwOmf.eyJNhnRGOdzAeIF.PDojmVQtm7V_swAYSuFynwt0K_ruYg1OXnaI/sse?token=\"8Jj8bLfxrFLjJf7zvduT0z9a4sqjEo6UkZSRNbs8MpK&session_id=true&amp;limit=50 HTTP/1.1\" 200",
    "GET /private_%C2%A0eyJn_VcuXaGLPBwhqe1TS.eyJFuqZbWfiWrwrSpsAPad.ZQf-d9h5sEhdAFAFPUXofDj9wCfqcqxDP4c/messages/?token='DEGKQX5pLW8H5+60lwiTcMvT%20%20&page=50&key_name=50&amp;name=\" \"HTTP/1.1 200 OK\"",
    "INFO:     127.0.0.1:5555 - \"POST /private_sk-a1bQ5lDKfd8yZ0qwi1iyRIH0/messages/?token='eyJYCOaTovOb2Ad6dogJv4Ea.eyJoTUl2SL9Uw2UmP_L2dkoDwanx.vrXuvHO1dW2ZlGO_3U_26h&amp;regex=a+b\" \"HTTP/1.1 200 OK\"",
    "/private_%C2%A0eyJaRpEVm9lTD0.eyJAlrl7C8LdSC6.tNT9dDKNQXj969Y5e0sC_DYx-KFSRqSfUwY-sZby%C2%A0/messages/?Access_Token='sk-hmTSfX4YqPDI35zqI0UBwb2angZVncVJ&keyword=a+b&key_name=&amp;name=1 extra words",
    "/private_525e8b1ae028f65ab9c3f2f7cf2fb5a0/mcp?public_key=true&&token='75e7c2db7be9a8f8eddce449d251372f5f922fd5ad33f900cf959ae040869fda&amp;Api-Key=sk-KUmbpzppvDKdI71FjBQua5MCuhRS_lzHB0jzGpHP6p7UwnLr",
    "HTTP Request: GET http://127.0.0.1:8080/private_0fa67b3e9a6db2324a0297929a537f24f7d0a432e0e1d578/mcp?Access_Token='sk-Ub2P9WLtROt0GDqATD1YLRuZaqKtq7iHlX5sZHnUHOYJNVjq&amp;public_key=true&offset=true",
    "HTTP Request: GET http://127.0.0.1:8080/private_eyJ0t7pYc7QBMpOMjY1P3fHE.eyJHYzgGCMq-BPbzorQGICpbsa.KyEYdqZXDsehw8T8ITbzeIPpPdZnd37/mcp?token='sk-xK4d64X2k4ANEZanlJM8GuCTjPU_5xQj&amp;keyword=1&tail=1",
    "127.0.0.1:1 - \"GET /private_%28sk-iEonb0YHuAdQ2FyOOmJ2u8ff/sse?token=\"sk-_HxSl1Ll_CdlYNi7MP8ittu5&Access_Token=%20%20a4e32043887441fd07858cff27fe26851e1b43c292026151f4896543c9fe873f%20%20&amp;offset=50",
    "127.0.0.1:1 - \"GET /private_eyJ%C2%A0r9I2nX2MefCFoC.eyJAj6Fo1HSpdtxL.HGKsQxwEIczDQusaRp5pvliFEM-4sM/mcp?Access_Token=\"ae6db15349b5ad19f7eb41b9ebcd4044&amp;authorization=%E3%80%802cgasoVnNtA9UGNRVk7CwTkj%3D",
    "/private_eyJawjJBSaL5VBdRrPmL.eyJ5kFgPHuG3fKSSbq.fCgd8dR9mVo9uheiTnQA44NUWfx-N2HY-8GaV/messages/?TOKEN=\"PjKFMqyElXTJe7xHzw0VKoj6=%5D&amp;api-key=oq9PBYg86Cw3uCwF2Ji5Jrp4rIFF7VPOyd-sVtqyhT1\" \"HTTP/1.1 200 OK\"",
    "proxy failed: /private_RpgZqzeEZrSSS0aZArdXjEpZvg%3ED9PeBxOY8AC%2FMC/messages/?authorization=\"84b2b68c520d339c90ff3ca0911c93ce&amp;TOKEN=%C2%A0f1b57a2701498d6dbab80cff59e9eee9ab8a8288981087ec%C2%A0 extra words",
    "url=/private_eyJP5DuzU4Pb3a30WUbA62BS9KPTbs.eyJD2ValuNZOdp7dl2elz5.aQlJB-nkmZui2UAmHZ310mq-EdfBVrFH5EVE3NQ/sse?token='eyJQUFcBTW8rarDXYR78Luk7f.eyJgCcUOAxIKa.d2UL0UhIwIAzSlD6STOBdnomEpiEzxIoB4Ozz%60&amp;keys=abc",
    "GET /private_%E2%80%8B8704b2d434a2c0dfba3420bc12f10ecee556352c2d489267618a36655d8511a6/messages/?access_token='sk-y1I-oTG_j2LKYLgo7fb2WN_7_bghPnNVF5Xvw6xOqkEmEptb%22&amp;API_KEY=sk-NNTsf6uY6aCETooR8nsIx6kT&order=1",
    "[mcp-server] /private_eyJ6Oxq4z5-ds_nfvaTIgkO14.eyJ6yCF6l7nKEtdkcn3Uu_9ws-vNWFKmorHd7NIZPk.DcycrAPlDiv8RwLeJNdUDwRNOZMNYaAnFaLtDPGz/mcp?regex=50&TOKEN=\"hGbbqt9FDVE4LIxxwqohmb2mm%2BPmbuM6Z4saccbn&name=50&amp;limit=50",
    "GET /private_sk-MoQxeAhDTMhisbicb6xnRNEdLAF4gg39-gphmtS-WuKe__ng/sse?TOKEN=\"99e85bff60a292f0fe04ab91a3b3f4b2%20%20&amp;X-Goog-Signature=%60598024ef3757cdc93af284854431c363194118f05ba38954d95b2628923b7d05 HTTP/1.1\" 200",
    "INFO:     127.0.0.1:5555 - \"POST /private_sk-aI7Uu-8CXZlvRpMktYcDdqUO/mcp?token='eyJMGn_8KRjWu.eyJBcJbsUyh8UoxKnYiQD4L1ORZpTjG04E3Arkm4n.TWhX4o-C8nmyPuck-q91QCpO_wSWMJH1Xgd%EF%BB%BF&amp;key_id=50&&author=true extra words",
    "url=/private_%E2%80%8BBaDquIgYI1R7fECjrx0vxj-IlYiPGzsL%E2%80%8B/sse?token='sk-ZfWYR5if9iwr-CCrEJIWlLC8&amp;password=eyJDY3lEVDNSpOeqJv22kevk.eyJNhzi7II1xVv aLgAHc32uBg7eOWpSsYAy7jv.KMbnMM_u8OBSIcnaFr7xmW3\" \"HTTP/1.1 200 OK\"",
    "/private_eyJ5TSGhBE0k_fvhyubXO1lNRaDUfOn.eyJhjYmftmb-pCWmVOLfk51lpqn7XRQPij.w3K9hq_s0gU8X8FmBIX0-Zm/messages/?token=\"b2dc9599c7e6af83d8acd4018cd285fe16e7d0226f56e3a6+&apikey=WW1XWdk6fkVw9JXmXH4x3vSCmc5LkpEx&amp;q=a+b&page=abc",
    "HTTP Request: GET http://127.0.0.1:8080/private_7968b99854d9a50c52e270e05bb66299293e3eeb1%EF%BB%BF8fda63c72735d45fec9a74b/sse?TOKEN='eyJYFsjISifKq4zIPjdP96YKUAECCoJk2.eyJ27wKTPcsH5.DX_RVp65aeEELCI2AYRegKUk5Hr9OBspA0_l&amp;name=",
    "url=/private_-0ezkdTSXUbmGR4IwuxEUu/sse?access_key=%5BeyJO88kk85CrDzI.eyJ9Qat6nyjc4-nacwPmHwPj1lszUz-zI31o1K.NXv-GrvbtKb0u74AvrSdCSJYuV2a71J&Access_Token='DXUgZFYLcI1FbhjuXh8hwu_d-MPpNNewKNePyvNSTNf%22&key_name=true&amp;offset=50 extra words",
    "/private_ec5W15CRD3EtiujltVpvcTKaJCeFN1Dst2DiOUli/mcp?token='z9WVrdoM_Rl_pfC4hZoYmC%28&secret_key=yFJZG5VkYd7pleoiI4W15Avk==&amp;regex=50&client_secret=eyJDNo76kyUcJb.eyJARXqReIygZz57agu5NtZ1Yu2hbF4qUlHhy.yikwlJNvDKvfNGAx7ILuk3eLrIULzz_qjT2Axq",
    "127.0.0.1:1 - \"GET /private_9c0486778b06b46d7acfeffc26cd5e66/sse?Access_Token='0136d030ac897f1772286805d08683f5d4765fa9d025d265c1620a54ada83f7b%E2%80%8B&session_id=a+b&limit=a+b&amp;api_secret= Q58mV0FGhFfuVVtfWGPPmB0PMesoJ954DYXn-zJrLCI #frag",
    "HTTP Request: GET http://127.0.0.1:8080/private_sk-7EJM-QletYDdJWXmUjiY4LFz_7rBYI75Kw_OT6u99NY4M9nr/sse?token=\"eyJJ2hDl3xGDXasip67Q_r6o.eyJLNIUC4s0OdhBYLLyvSxbO_mnLyfCcqLr-vUBqW.5IIeV1DsbDCUg_wADNBPL8kAA2So8rEUoIF1d&amp;session_id=1 extra words",
    "GET /private_t7jeOM/uY10oz/mxPu+pMvkGmA2sXude8MVXdcGo=/messages/?X-Amz-Signature='ae6d01f111b23ce82d96c502d87d9e45fc7e371b2f395d76731f324e291a05da&amp;TOKEN=%22eyJ0WYUG-XDSMJCe9nrj9UJ7sr.eyJTA6hBql1wi.4_lIwGdKSJzozvGmASoWs_aUuTeEcKI0ufZPUcIdqVB%22%20",
    "INFO:     127.0.0.1:5555 - \"POST /private_7eZ83+SZ7CXhuD8hdkSdAiknVj6wyK8JV/sse?public_key=1&token=\"sk-9-iUvJbdjyHHBvX1wPKG6l9Y&passwd=BxSpen9Q3s8cIlRGWTili7Ee65HTGeeJ5y-Uko4t-Jz&amp;api_secret=eyJqZZWEckJuWz.eyJGZmLWfyUbVtwdPXEX-6.2JtzYiRg_pMW02JJa2IOmz",
    "proxy failed: /private_GzZNCB8Ntkjpfyyw6iA5fM/messages/?token=%27vjjWsQZ5JstKvbRfpDON3Y%27&Access_Token='eyJW027maabn9brlB17cxWq2.eyJzLEfTQbht7KF.Ip3aPhh51v3ECWQkTsdqYp4aqI-kVeHmuDkypEO%22%20&name=a+b&amp;pwd=sk-9SP7xXdjOpzcGn-LUwSHiWB3y2gc1xyN1Be-uGLa0ZHCXcR7",
    "INFO:     127.0.0.1:5555 - \"POST /private_sk-EXHwbn28x5tB2iQfll8WwsIE/sse?access_token='eyJuyElSRo9WXdAsKXyrETybsX.eyJiYZr1ja90kCK69P3dJUpP1DxhAEiUP.VDlUkk7jGj7iKvdWkeAJSnOZsiYOnMyXypztj&amp;password=sk-vrc5L4qik0jnrni2xfvnWphjIw76aCGuZ0XQ7ldLpxoJKs_j&&key_name=50&amp;sort=",
    "HTTP Request: GET http://127.0.0.1:8080/private_%5BeyJ46kHPUmJOE-.eyJeqeqV3dtiN4AFHxYJ3R2gFNJK3gXYyTNod.ME10KG5l06yXG4VkuC_O7Ev4mKCcyS3_U/sse?TOKEN='HRytBuOY870ShXOlidBDogoO%E3%80%80&amp;api_key=%E2%80%8Bsk-crObSVYQ5JJ1vzWGjIj0YOO7H-5Pctj9QwKkgV8szCby8Xi3%E2%80%8B&tail=1#frag",
    "/private_KuWVW0NC--u3pMTM5LAJLV/messages/?token=\"sk-DhchJFjN7aT6BO4tx4uZ3l70&amp;sig=eyJNSKk9oAsYXiOtl-.eyJtOx15_UiorUAwXD12_JmZVI6P.LDBnTgV-haKtZylqaCc-4mm-L2PPP&X-Amz-Security-Token=eyJQva0vvWo-UboHg4aEU.eyJdAaoN1vwzMU_x2gznPN7SrclY8.mGOnmPsOE_afmSukpUq12e_2jR0mz extra words",
    "[mcp-server] /private_o68twht7gV790VE3lZvruuMf/messages/?Access_Token=%22%20kvZ_RU026xLjIqvFV5lRIosi1qlG0Mz8PQkMFs4qRUZ&access_key='sk-IWYCCO6PeStlp0xEMoOB2c1IBpsjbbwNk5KSx9MXT2h2Vdtn&passwd=%22sk-UjmB7TA_OSZSDSN00ElN6dLB&amp;token=%E3%80%80sk-wSUl0gMOUhGmASOgJEytk5lXRSakd9UX%5D",
    "127.0.0.1:1 - \"GET /private_eyJtZbULEyWrxpJ82z.eyJcq6f40lvWxhhuQGj7BdTjkdewQjCzm5qLEOdy5Tz.YHbPBNGcY-UrDsq3FSHcqJ/mcp?token=\"Y1evcf_9Oh0ci_biovqtWd%E2%80%8B&amp;sort=50&amp;access_key=eyJZCPmVPhW9fKqY1kSaiG1K5H4JLwb.eyJDaFi9EOvVd0jYSKh8AopI-X37OjvJXzFdCnyG.o5TFlHUBHBCxJucEIXS0S945",
    "GET /private_eyJ2xsXthpDXoJxuraZuwZZumZFwv.eyJy4Xdy1dmL_cuHr_0mieyTRz8EUqAluw1U5c.zke4ElnCOy0oPTDUJPzZbtmjFz6kx9Rnn/sse?key=\"eyJmQ2dWfNyHoOK3Gx6Q57tsJy.eyJKoRMgjrpOALWEuZjuk0hZ.7r-oQlTYTs8NpNmHC-JOzck06_aSMmm&token='Y7gBs-u8RdGSROO_dvVSmXqOCpePVL-h%22%20&order=a+b&amp;session_token=50",
    "HTTP Request: GET http://127.0.0.1:8080/private_203634525a2f3f9fa21fcc70647795db103312f266906d5b/messages/?token='eyJpJapUoi86X0iz5z7KuHQjUV8IOs.eyJ8IUjF7XfaO.c_VL9Bno2dXiD-Wf2zw9%22%20&amp;access_key=yjhhHIfNBR%20tuka0P-ZxybZ1Dd_Mo0lxX;X-Goog-Signature=%5B0f4P0zOpczs7lQn36oo7ULoOWnGMY2yv",
    "GET /private_5SDyhLFq08GDmdfJpLp0ybtkhstkLDaA/messages/?x-api-key=d235bc1656b927837732e5010d9f27da;X-Amz-Signature=\"eyJWqW8d1yw82-6q65k0Pbdmd-Yrwmehk.eyJMbMl4yFU9HP2i7WIneg4K9ezFyN5.3Lj9LXUgbDI-PEkvs0O6Tz1dEcHeEg0rEJ0;Access_Token=\"sk-yXdS5fMKlGKW7AtyMbJc6kcH&amp;passwd=89AWfOm33vWWMrntji5LzX8C\"",
    "[mcp-server] /private_V2yVTf8fLP35d3IBHhIt29WWU%2BJDki0TJDF%5DS3w9q%3D/sse?X-Amz-Signature='c626d4d2592c8ea0f9a2dbc1c5f636668cd9993751fadc49&X-Goog-Signature=eyJyAtS-ZVwGLY3vZ6q77rG0EyBi.eyJ-lZnBgAQ3roXbRw-7NY32JrC5mzTJiZkob70Xt.UXYCaQ8EG5C_dlfdaHun_Py&amp;TOKEN=%C2%A02w6ysifrR173FQINzG6Y7y%C2%A0",
    "HTTP Request: GET http://127.0.0.1:8080/private_eyJEtLLR6wMKK6-KW8y8Psf7kJDc.eyJ0Po_3YS_UL-ksDsZGso_nSWgKAM1pWGp.y55cnxyWNj4DP9QwXjIddW3b6vF8ozs3hWj/sse?TOKEN='duRb5g9YDESHQJ3137rKEEZX&amp;X-Goog-Signature=%20%20f5281b8dd4ea645313e5f025fb085eff23309a0fd0eae6d355ae653751e85a76 HTTP/1.1\" 401 Unauthorized",
    "INFO:     127.0.0.1:5555 - \"POST /private_sk-ACegfQ58YKsbME9lJ55YCy401dYsXi4I-7GTHoPtRa2z3WBg/mcp?TOKEN='eyJ621zmEXRlbIRmxy.eyJ82lteJ-y1nsoddz2aihrMHRjOV5GrNM.JxPlTYLppo-ePVeRlord9-CjFBczA56zX3QHV7%27&api_secret=eyJ4vrSChheJRpdO_jgkqd2u%3Cil.eyJcs_zdXBCxSDqXOsOcNYa_j.eIzYX4Oo83EDZFjuq78LsMWOCHiqPTGdpPD9&amp;regex=",
    "/private_7ebNQrcG1MJjm8PUK+UES3EOh/6N0mDvyDptsX24/messages/?TOKEN=%22678f00d121ec5b2282fb36a44fd24c5bf2f292621b9ea4fca2b965eda4b362c5&Access_Token=\"sk-5r0YjmHfFcMYT3wKIOs3N5OBwvGyAxwT4xyRjfNZ_JSpBy5Q&amp;apikey=%C2%A0eyJ1KryUF_laCkofnV8L9y2.eyJLoMa9U1GbXu-FZdwGRWWxbM.hn8RUfQgbmMeVgCBOqUItHcNvtx9nu9Ujv%C2%A0&session_id=50 done",
    "proxy failed: /private_%E3%80%80eyJMChMl6-zau.eyJF-8RuwA9vagi28vH-y1ILCs24jrcqjYH.3ulzDvoGulFYd9YkqZ4d%C2%A0/sse?api_secret=\"eyJHtE0bO9xK5AvCxq6kTLY4fmwZlCL.eyJRCvt2E26S9KuTyOAH2FhGGXIEbk.9sg46kBJ8QrQg9T5JBi8%27&q=1&amp;access_token=%22%20iN7m_wfRrb0mWO4QwHbpwAFiji3-I2T6&client_secret=cp3QNo4M6+iRi88zSvItlPTTcvE5W7rOPFteg3ra==",
    "/private_8223c757e%E3%80%800de2a8cb1c4bb3de0317e1a/messages/?sig=sk-M1qj4pa-ehSPoK4%22wqkVEnAYy-BXgwhZf0Q6Ed2eUwJLzWH00&API_KEY=ZdgZrEc4Vp6dmyrGM_TDAVcE9xo-qyht&token='ee0dc9ba7ad6b03efabfc36a92a23148f9861c2c763ebff4%3E&amp;secret=eyJlvOnRu8YsfPmm8h7kHBiGQ_Z8wTYBe.eyJa-AmqsX7rC3KLKAv9dSGCK.41bcUVrzBuCp1XpXODxbQSqwW4z6K_gj0T7coMGx4Cp",
    "GET /private_eyJcd4pNSakJw-Y69is.eyJqwwqPIv0SjPHrD-lefe7xke.ONtWXlJOZHhsnwKp0qjKWQ9IrceQj0dPqWA7Zp/messages/?api-key=eyJItITCxaJsg.eyJFfPGr7oLec_cJgCImpuSDDVtMQ4ohrnJCgwjzWS.2MWyustCW0NvXs0dhmsun5YST75Nh-IN5o5q&token='eyJYHa1efGsfu.eyJH_sk8oXVRQLzYPMX2-X_-7HmC.-DlOMPl7fX6unC2FzHOOGIg5_3WUgtE9&key_id=&amp;x-api-key=sk-MO26IY3pvZxRC8kzpYKSo3jc",
    "INFO:     127.0.0.1:5555 - \"POST /private_cc0a84d0b9ab6a83600fb0bb8cbdeb621ee08dc2d52124b4/messages/?access_token=\"UBkvBoMNsUdtKwzJcAtr4E2VspqkVIv1VzuDjdNWrSk&Access_Token=%27sk-r4rqs2ZXYNfWx5jkldCrXe1gg7NZrmFR&amp;sig=%EF%BB%BFeyJZsfby6uvP2TS-Tc.eyJudHc7-NCdVwFd2P78t0xByY.pl1RaFabOmO_JIc7KHSBnYeycM%EF%BB%BF&passwd=sk-6X1AZry2_CQyKYfwQGvxqKnS2-uhbwoy",
    "127.0.0.1:1 - \"GET /private_Qj8yj5F-b3hpROrdDCWFRr/messages/?token=\"eyJANXsrlX7Ai5PBxiHTFEVuy7ytj.eyJgWD4M-aCfXep.RaHOokHLalpZ9xFNkt97y-JAUHCPh5N2cAd%E3%80%80&amp;Access_Token=%20eyJ430LMnJcxDUZMJy8Gjm.eyJmgSRGM_POf1ylLELW8Sj9-5.aFrDEqyvlswvmbKVtwkb%20&pwd=%C2%A0eyJT5FkQorokvhFtjOE-Gg8MYbQ2D0e.eyJGnWmzFeMZ4R91V-7UJ7ncsOe.NTuTJvN0wpElF0jr2PSdGufNjs done",
    "INFO:     127.0.0.1:5555 - \"POST /private_7f7445c0128074f146735f33b416cb18668313f7baac90bc/mcp?private_key=sk-lL2r7ZYe_qdpafJEBv8b_NX-aa-958Qa6Edl5os_NKrU7dSZ&&TOKEN='sk-YCsO1jny-tUn7lvjNIR0PnkoZny40yhYmc63pUAErf_5FG06&page=1&amp;access_token=%20%20eyJyTpUlLgDeEgdxk5ZN8fBK.eyJJtoUEpa5olb-wMxhO-6qn6-A9hvJl0ICLkN-.PV_H60InCN4iffqOoJ7vf45YxRl_GUnZHYMPVoR8AR2%20%20",
    "GET /private_0c926702ae3ff7fe322d5127c3ed1db2af1b4a5479f45655b5e4e0f1625684e7/sse?x-api-key=baef2949e203f28e76168607225bd412ff117b089c85bb8e&api-key=+wL7O9486KEOfJ4Y0TiGdmZ+V=;access_token=\"eyJjt0NnL0b_jLZz7Err.eyJiJfbE48MVwFIWINOYHqk9p2ds1d_bf.48GhSZUocDRblwXBlgqrZxeEp%C2%A0&amp;secret=eyJnhoY7KiM77NClqkTTUqI11aBvNT.eyJimAcYGx6Sol_i4rxq-I8ds3HJQJZt_jjq_PINE.vTrw1x247K0nkOcu-BF8NMF3dHX9IqfORKgFT",
]

# 自己的差分 fuzz 在「沒有舊規則會動的行用寬的規則」的第一版找到的不冪等輸入（第一次的判斷與輸出再遮一次的判斷不同）：
# ``private_key`` 這種鍵名裡的 ``private`` 不是前綴、鍵名只有解碼後才認得（``acc%%365ss-token``）、``token`` 鍵被別的遮罩吞掉、
# U+FFFD 緊接反斜線再接 ``private_``（拼法階段認得、解碼後的字面不認得）。
IDEMPOTENT_WIDE_MODE_FOUND = [
    "&acc%%365%2573s-token=%3B`Q2nb7BHV9NF7",
    ";a%%363cesstok%2565n=%3BxBoj%63RVRoN199yE%49",
    "&access-token%3%44%3BvWuSNbQn%2558aSaCdG0%2B ",
    "%3%42ACCESS%2dTOKE%25254e=%3B5O3Xjdp%72B85Pm-XJ",
    ";access%2Dtok%65n%%33D%3bqeRgv%64IAUjTFoc5%2530",
    "%25253bACCESS%2DTO%%34BEN=%3buVdqKLRxnN_rdItxCGQh",
    "%%33BAccessToken%3D%3B%3fpi5Bz%26Xd\ufeff8uRn%25%33%46j",
    "POST ;%%361ccess-to%6Ben=%3Bi1V2%61LUWqVvNnV%734%20",
    "%%32%36aCcE%25252573sTOken=%3BtgrXGPjwR%52ueUcG_cLwA",
    "%%326acce%252573s-%74oken=%3B@ML%71%52b%6eYL%4B%48jN",
    "GET ?acce%C5%bfs%2D%25252574oken=%253B-z5aHmbtU%67Xy",
    "&access-to\u212aen%%33D%3BFlT8%2570zMc%2577Jn5%2b%2F%257C ",
    "?ac%2563e%c5%bfs%2D%74oken=;73aMLfCKRBK%34-%52%%357e\"",
    "?jwt_alg=abc/x/?ACCE%C5%bfSTOKEN=%3bBfC0rrD-Lq%37Y%29",
    "\"&acces%c5%BFt%6fk%65n=%3BAB6zzm-dTUID%25254bscCknE5\ufffd'",
    "&tail=&;%41CCe%53sT%6%46ken=;%2FOgZ%252554Fy%5FNh8%7aq",
    "%2526A%43CES%C5%bfTOKEN=%3BPR%62_h670v_%252567S%%32%31/",
    "%3%62accE\u017fsTokeN=%3b1%70HGcezBp5y%252558JCu9B%4FSRN_wE,",
    ";ACcE%C5%BFS%2DTO%6BEn=%253B%3F\u3000fBFXvxBQOY1T%2a ;page=\"",
    "?ACCES%c5%BF-TOK%45N=%253B%2b%EF%BF%BDZXvkM_N12Zs7%255e",
    "?ACCe\u017fstOKeN%%33D;E%68%25253a@qO%52qW5RKDxQsvrXqx%3%33'",
    "?accesst%6%66ken=%3BGrzp0ipDDl-wAa-k HTTP/1.1\" 200 ;q=1",
    "\"&access-to%e2%84%AAen=%253bmy%36pv9GAlzh%%34%64%E4%B8%ad",
    "%3b%61%%36%33cess%2dtoken=%3b8Iyu0%69P%6%357fG%3f5Y48h%20",
    "POST %26access%2Dt%%36%46ke%256e=%3bcDbzYawirpHJN%4820%c3",
    "POST ?A%%36%33ces%73T%25254fkE%6E=%3B_6%%35%41%69derjPFS7",
    "%253f%61cce%73s%2574o%256ben%3%44%3Bi8Mw%5EfFoBhVU%252557%67J2%254A%2F%3b%22%22\"",
    "- ;to\u212aen%%33D'hoE%6FFaI9st%254CwWlcRbFARDVR5Q%25252551ww%256ahzt;;;sECreT_Key=abc&x=1&%3b%6%41wt=1",
    ";ACceSSto%E2%84%aaeN=%3bPUr9%480-w9yD%2578IK0%34%2F ;si%2567=pE7r%705EIfaAi%2535IwCy7WlIB%47-c%54MPdFAJ HTTP/1.1\" 200",
    "?acces%25C5%25BF%2Dtoken=;uVpX%252555UK5f5%2545k%68Y8QKgJp_%2525469sSPN%34%66s8e%25EF%25bb%25BF%E2%84%aa%2b;;?api_key_id=%E4%B8%AD",
    "?ac%63es\u017f%2dtoken=\"am%69_U%256c4Lwse%252531khuzPf%257a%2534LbjjwINSfIlN?KEY=\\?key_id=1\\%253F%61cce\u017fSTo\u212aEn=%25255fay3Sst%2525397%252569h%63gycG",
    "url=&limit=1%20?clie%256etsecret=1&limit=5&%253baccess%2D%74%2525256F\u212aen=%3blTaFlqk7se%6euq%31qRZYIgv37B%ff?;api%5Fkey=abc%3B;to%6Ben=dmneq%657%4FhkPeGUQL",
    "127.0.0.1:5555 - \"GET ;oauth_state=1/?tail=a/b%22;AccesS-TOKE%6e%%33D%3b%253AdMRsr%387K1%67QgfXK_nA6xHO1dT3T1tuKQN _tQB%4fwwq0%252568HNGWS7y2bMX%35GAWzI-9me%20#frag",
    "\";Acce\u017f%53%255FtoKe%4e=&amp;%c2%a0AD7S9Kq*%25%33%46JmAn8aj92%30%256d%67Zo-Zb3y%37rg7BRycT%7eXP<%70LU&=?ac%63%25252565s\u017f_to%%36%42en%3D%22%2525255c%C2%A0%2fDhS7rEXmsu4rXC8%6c\u00e9",
    "url=&accEss%2DtoKEN%3D%22PY-o%720%46Po6o%2572e3%%36%42y%5A6H%64RprMPaiJNFMpOPMwhhRt%3F%22%20&ac%63ess-k%65y=6V%6CK%69z7pAl8C7K%52NpBgk4XjD:?ac%2563ess_tO\u212aEN=%4eo9As%252525379bZ__%2579",
    "POST ;acce%73s%5ftoken=\"\u212aXU_N%4CIGT%2568Tf%2525256ef%585UuldP72TRjCyBKEV0%5C?signature_version=a/b?&%%33%66X_AMZ%2DSECURITYTOKEN%%33D;;?a%63cess_token=4hnQwvSB_IQiJ2OF%75%6Ezhit-i ?keystone=5 ",
    "127.0.0.1:5555 - \"GET ?sEcRet=x&x=1&&TOKEN=%22%5C%254fu6%59%56GJZJY9z%424%EF%BF%BDuo%39Beiwg%44%2567G5WqfymYj%252547bST34W%3D ?pwd=abc;%253ftoke%256E=B%2520Bv%2520-{%25%33%46%256b-on%52Y%78Mk%254a%39%6c",
    "msg: ;jwt=;;&auth=1%20?access%2Dt%6fken%3d%25253e1C93OfDMox9%5253%78g%73gse76dp%2538%55QJ%252542JyuhCt%4DW%32%7%30%36/&toke%256e=%22%2aX%31uxfazyD%2545wtDjn%46gTY%62X%255fdkbT%708-e%%35%39%37Khopsr%33s%e2%84%aa",
    "%3F%74oken=%27ATkPmfptW%51GF%3B;session_token=&&x_amz%255Fsecurity%2dto%2525256ben%3Di%64aX43vqQp%%34%33xygGjGyFif%56F%256D?acce%73s_to\u212aen=%64O%4Cd0IX5kwAtS%4B%7a_SP%e0%80%af%252CCUKRs2%30gkAlM+%22%22h%53T%46%2561VMMat`",
    "?to\u212a%252565n=%22%25EF%25bb%25bf%24e%70lLofFqJvVtFr%47xancOVAK%4b%6b6xQyjhx\\;API-KEY=%3f%3Ere%e2%84%AAG%2546%2552W%%342arxA%69/+;token=My54%38_W7T%50WyjZDmDAJTm%255f%2555%2525255F;;%255cprivate_ONA2aC%77M5B9Hrhiz?sigma=%E4%B8%AD'",
    "\";mytoken=&?to%256ben='%%32%37%2f[kb%2565wbFOh%320%255577oYh%25254A%25252556e_-Y9S%6F88%43w%252546YEhCgpfPR2MTWwdji%49ZU%25256eu1-7%5390zXfIoM%C2%A0%26;mytoken=a/b?&%3fsig=%3B?token=K%256Fg2%54Aq%70%5av7kpWi8C%2567JTQvbGTHLChwpKNozR9fRN ...",
    "&%74oken=%22zWjW%5fih7G7wK%%36B%c3%A9%25c4%25B1AbW:;sort_key=x%20y ?p%2577d=c4SQZKjte4puCm1tyv%2561%25252531TWhF%25558t05%48HXeAdUQq%2533gvqo%413%782Dak%2564M%%35%464ERFldNm5_4%5C;Ac%2543es%2553%5fToken=s%51Cm%254EPzE%39iLU%2572VboRoftp3IrC9%55yWum%35",
    "- ;acce%73s%255Ftoken=%27~%5clob%65Dk4RNR%25255f%794d%58N7a%4eI%2C:;passwd=9z-Bai%65nlwNQ%6Ei%4d%59a%36rk%2532%61YR%3B;pr%2569vatekey=>B%49%2537_3\u200bJeuG5aTB-%38iSAnV\u017f#?password=8%%35%36_-_ISPfPY%42D%41I87cuX4D%25%33%46r%2525256FyRV_w%%37EzmG/?password_hint=a/b ...",
    "- ;AC%43E%53S_TOKEN=%27wI3Y%76HMX7pk%254CT9T5%457WS?&SiGnatU%72e=x%27%3B%252574oken=%3B%6Dc2JDEh%%35%37%252557xV%25252533NDcGDz4q4%252545D%252525786%44j%2564%2578luORF%25455%35i_OqBM%252570j%61Z%4bFD%43%31NSG%2576j9a%78%646%64c%2f \u3000 ;oauth_state=\"%2526ac%2563%65ss_k%65y=abc",
    "&ACC%2545\u017fS_TO%%65%32%8%34%%41%41E%4E=%27%2567Cn2FkfBxNBD7qsVvp4VIViQ_-2orEW%2542AzaKgrXT,?PASSWOR%44=-gX%6CM5D2EgSsG%58H%47ZsP%252554%252537Iv2%5C%3FTO%254bEN=%e0%80%af%e0%80%afG%45v%2FzqV%e0%80%afHFIk%2552aHPkdWs%2575o6qFFSCACD%30dc%2B%22?signature_version=5&authz=#;auth_mode=x%20y ...",
    "?auth=%6CXD%254bn%33%4d91%487oW9dqfHp%57c%36%256dy HTTP/1.1\" 200 ?acce\u017fs-token=;+%25E2%2580%258blG%2573DEkDnuSrMNA%25252572EWe6j &Se%2563rE%2574_%4B%65Y=eF%2573%255APasANvdS%ef%bf%bd%7C%22%20?TOK%45N=%%32A%2Bytd%51qF\ufeffB3Vcs!%252520tS%c4%b0%26Jn0%2dUmp%252dHuQxj%25252567q2%2541%25501+?signature_version=",
    "&TOKE%254e='0%7A%2551%5f%7843Hy%25256c%2559pjT%3BoYv%2565jmxM6K%26;%4AwT=%ef%BB%bfWCQ%21%e2%84%AAYbOAZHXvVXpJ%7381es%5C?token=%7e%7b_OvYOXcJu%6e%33Of%72Wvq51w%78M%2576-3u%57GRpTGf%25252571sA3zNBB%50%2541tG%62r%256fB6b%%37%34_o%315a%254BAc%%36%38m%73v%25252a?&?sort_key=;?accesskey%3D%2FgSyfOUCUtkVeV%2548BG%252578ml%41 ...",
    "- &%4bE%2559=%3F?x%2d%61pi_k%65y=_-S%25256D%48%252576%%37%384%25252579IHU&x=1&;AcCess-toKeN=%3bsPXohmQ%74U%4A7eK%44%%342%252570%76FUiWE6WSh\u4e2d&amp;W%32244h#&x%255Fgoog_signature=O0%58Y%41%76F%3%357Y%47O22d6_R4%2553j%73b1%2566gaaq11J%7AyZ%55x%2549te:?TOKEN=PrME%%37%37vuO%6%38GXlZAT-PMSZ%437oGqGwr42Wf6\u00e9%25%32%30uX%2525E3%252580%252580%263Z%2576hG",
    "127.0.0.1:5555 - \"GET ?authority=%E4%B8%AD%20%25253f%41%63CeSS_t%256Fken='QEI%25252d_BLu%2525256a6Z%41nLS5rag%2556%66zRuO0-k%6f1%61-;;&S%2545CRET_KEY=%2a%22%22%C2%A0T%252540Lu1fOO%48%2541%58%4cu%e0%80%af%20 ?jw%74=DLW4%53%25704Sy%2558yF1%2568d%252552Q%%36%38qu;TOKEN=Oskt%2DpR1J%25252534%252569L_W4MXlnfy7dwxZsu%6Aj%50xdkunwuLZBZTmVi%2577bMy-%34Hp%2525644rgK%%36%639pzE",
    "GET %25253Fkey%%33DNyIG2yVqu_c%252564iiH%%351 HTTP/1.1\" 200 ;s%45cret_kEY%3D>%ff%25%32%30hHI%25252556go8okK%254bcMakh%%35%31%2533jh%252551qL%6EI%4D:d%2b7YA4e%257d ;access%2d%2574o\u212ae%6E%3%44;%ffeH%25252561g%32PioZUgW4oJo2%78%59%2530%7%33bn9J%6CGiAm0d_avioCe92%6bI%527%75vF8BXTeTf3NArlCoa8%2a%20%20\\?x%2dgoog%255Fsignature=n%520dTiHG%25481efX%25252532HEl%2567XJL%54PKpp_cpf%79hBtv0%73JS4%25%32%30%20\"&tail=50",
]


@pytest.mark.parametrize("text", **_numbered("lead", [
    f"/x?token=a&api_key=&?q _token={SECRET}",
    f"/x?token=a&password=&junk to ken={SECRET}",
    f";token=*&api_key=&?q _token={SECRET}",
]))
def test_bare_param_masked_after_an_empty_value_can_become_the_lead_of_a_loose_key(text):
    """空值後面的裸參數遮成 ``&***`` 之後，``&*** _token=<值>`` 的 ``&***`` 加空白就是鍵名的 lead（差分 fuzz 的密集產生器 6 百萬行 1 筆，
    c3443a5 也有）：遮完裸參數要再遮一次鍵名，第二次遮罩才不會多遮。"""
    once = lr.redact(text)
    assert SECRET not in once and lr.redact(once) == once, (once, lr.redact(once))


@pytest.mark.parametrize("text, shown", [
    ("/x?password=abc;token=def&limit=5", "/x?password=***&limit=5"),
    ("GET /x?a=1&secret=abc;token=def HTTP/1.1", "GET /x?a=1&secret=*** HTTP/1.1"),
    ("/x?api_key=abc%3Btoken%3Ddef&limit=5", "/x?api_key=***&limit=5"),
])
def test_token_key_swallowed_by_another_value_leaves_a_line_that_masks_the_same_again(text, shown):
    """有 token 鍵的行，token 鍵被另一個參數的寬值規則吞掉之後，結果沒有 token 鍵、也沒有前綴：第二次遮罩會把它當成「沒有舊規則會動的行」。
    ``_redact_bounded`` 結尾第一次就把那一輪做完（``redo``）；目前找不到會讓這一輪改變結果的輸入（見模組註解），這裡釘住行為與冪等。"""
    assert lr.redact(text) == shown
    assert lr.redact(shown) == shown


# U+FFFD 緊接反斜線再接 ``private_``：拼法階段（``%255c``）認得是前綴的起點，解碼後的字面不認得（各版本都有的漏遮）。有 token 鍵的行
# 第一次用保守的規則、第二次若把這一行當成「沒有舊規則會動的行」就會多遮（不冪等）；兩次對前綴的判斷必須一致（``_LOOSE_PREFIX_RE``）。
IDEMPOTENT_FFFD_BACKSLASH = [
    "GET ;ACcess%2DToKEn=\"%25%32%30GMehC%2525637ad3q%254cR%%332BJN%788%30qoxp\ufffd%255cp%72ivate_EoiiLoGKEE%25252567q_%36e5%6d6p7O%7343"
    "&limit=5&%5Cp%72ivate_6FE9QF%256f_gFCAB8VCmYyOrBZ21OhFyRw3/mcp",
    "\";TOken=%25e2%2580%25A8%3fVBP4FGum4%252544%2564SA%2536nJJ-%4dkIu%44%254e\ufffd%255cprivAte_0yYP4x%4DELQtg%70xUx%2525744KPPNBk_aDQftuWT%61E%33xiEw"
    "?/%E4%B8%AD&/pr\\\\%2569vate_swlSZ%66GOD%252566VL8%47r30Ey8NSzB/api/logs/search?limit=1 ...",
    f"x\ufffd%5Cprivate_abc?token=%20{SECRET}&q=1",
    f"/x?token=a&sig=%20b\ufffd%255cprivate_***/mcp",
]


@pytest.mark.parametrize("text", **_numbered("fffd", IDEMPOTENT_FFFD_BACKSLASH))
def test_prefix_that_only_the_spelled_form_recognises_is_judged_the_same_in_both_passes(text):
    once = lr.redact(text)
    assert lr.redact(once) == once, (once, lr.redact(once))
    first, second = _scrub_twice(text if text.startswith("/") else "/x?" + text)
    assert first == second


# 前置遮罩（乾淨值）把值連同尾端「解碼後是 U+FFFD」的跳脫（``%80``、``%BD``、``%EF%BF%BD``）一起換成 ``***``：U+FFFD 本來擋住後面
# 反斜線開頭的前綴（``_DECODED_PREFIX`` 的 lookbehind；各版本都有的漏遮），``***`` 不擋，前綴在第二次遮罩才露出來被遮（不冪等）。
# 前置遮罩若讓解碼後才認得的前綴露出來，這一行改走有解碼後前綴的規則（``paths``）。審查後的 fuzz：1800 萬行 5 筆，換一組種子又找到 1 筆。
# 前 6 個是縮小後的最小寫法，後 6 個是 fuzz 原本的輸入。
IDEMPOTENT_PREMASK_EXPOSES_PREFIX = [
    '&auth%6Frization=%af%5cp%7%32Ivate_s',
    '&accessk%65%79=%BD\\private%5%46a',
    ';KEY=%BD\\%%370rivate_e',
    '?key=%ff\\privAt%6%35_%',
    '%253Fpasswd=%bd%5C%70riv%%361tE_%',
    '&ke%79=%80\\p%%372ivate_g',
    ('127.0.0.1:5555 - "GET &auth%256Frization=%50%2543FbZ%255F9Bg_lv0iaDuVV'
     '%25588a%2532F%599s1nTU%30GnZ6ibDgZLX%2561kaxLv%2573%5AYuJDj2bRhq%32my%'
     '25e2%2580%25A8%e0%80%af%5c\\\\p%7%32Ivate_OtOSu%7%32B4vEI264s6iPlDSHCs%4'
     'E7%25252575kCJ8o%63-H%46N%5fh%4E%47-IL_X%72hcN0R%47q094X65PDoc/api/log'
     's/stream'),
    ('GET &session_id=5%22&access-k%65%79=rV7%51hIrXGC%e3%80%80OQ(%EF%BF%BD\\'
     'private%5%46vja%256eSD3-t9LHpaoxv9%34X%2df%256fvxqA7sbSfD0TG%77bkV/api'
     '/logs/stream?sigma=abc'),
    ('msg: ?sigs=abc&limit=5&;KEY=%252562h%2b3OMjF{%20i%C4%B0vrY%65%EF%BF%BD'
     '\\\\%%370rivate\\_e5%74%67%2574%2550B1J7syDC7e/api/logs/stream?session_id'
     '=a%20b&limit1&keyword=a%20b#frag'),
    ('%252Fprivate_%ef%bf%bd%5CJ\u200b%252520sA]BB"HGa5%252569Ibky%2d%38/mcp%20?k'
     'ey=Gh%56_%2570W6EBjhaR%2553DpLhT%2565%25E3%2580%2580%25255b%ff\\\\privAt'
     '%6%35_%3FJGRU%61ska9Q%21RPQlgaBn0W/a%20b%26/pri\\\\vAte_%25679fhU%784I7H'
     'Akrwv%256E%616w%25256a%2552fJ%6%65%2553i%7D%2553%6EUhvl[%ff%25e2%2580%'
     '25a8/%E4%B8%AD HTTP/1.1" 200'),
    ('GET %253Fpassword=%22%22%25255d%25254fE-59kiE%252555d%66%255A9j%252565'
     'tII3ZPx2K9%53%6dqCa4d%252571RSNk%43f%71so%62GJ7TI|hl%7003HcKA%252561a%'
     '5A98%2537h%2529%ef%bf%bd\\%5C%70riv%%361tE_m%54P%25252571%57Y%38%5AJpcK'
     'v%252552%252576_DYIzyOE5/key/info%5C;authz=a/b'),
    ('POST %255cpr%69va%74E_%3F&amp;qQBM-e9os%2D%6Cg%7276Q7S%6A6kiFyTG%25255'
     '2XN%252564%381!\ufeff/mcp/?key_id=;jwt_alg=1%26secretary==1%3B&regex=a/b+&k'
     'e%79=+hW%39KR%252570B2F%25700D%6bf89%36f1GPebM%80\\%255cp%%37%32ivate_g'
     '5bZIdjvIJ%6dTX%41FswOXWu%%33%35Zyp3%256e-V%31%59%76jNHvI4FUBo_eKmt%46g'
     'C%79N8C%72citFB_F%74u%2520/api/logs/stream'),
]


@pytest.mark.parametrize("text", **_numbered("expose", IDEMPOTENT_PREMASK_EXPOSES_PREFIX))
def test_premask_does_not_expose_a_prefix_that_the_decoded_rules_could_not_see(text):
    once = lr.redact(text)
    assert lr.redact(once) == once, (once, lr.redact(once))
    first, second = _scrub_twice(text if text.startswith("/") else "/x?" + text)
    assert first == second


def _scrub_twice(path: str) -> tuple:
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5555", "GET", path, "1.1", 401), None)
    lr.scrub_record(record)
    first = record.args
    record.args = tuple(first)  # 新的 tuple：指紋對不上，一定會再遮一次
    lr.scrub_record(record)
    return first, record.args


@pytest.mark.parametrize("text", **_numbered("idem", IDEMPOTENT_REVIEWER + IDEMPOTENT_WIDE_MODE_FOUND))
def test_known_non_idempotent_inputs_are_idempotent_now(text):
    once = lr.redact(text)
    assert lr.redact(once) == once, (once, lr.redact(once))
    first, second = _scrub_twice(text)
    assert first == second


def test_redaction_is_idempotent_on_every_corpus():
    """``redact(redact(x)) == redact(x)``：PR 自己的兩個產生器、上面的家族產生器、N7 的 97 種寫法搬到 query 值上的矩陣。"""
    r = random.Random(20261116)
    corpora = []
    corpora += [_query_text(r)[0] for _ in range(4_000)]
    corpora += [_segment_text(r)[0] for _ in range(4_000)]
    corpora += [_family_text(r)[0] for _ in range(4_000)]
    corpora += [_pathed_text(r)[0] for _ in range(4_000)]
    from test_log_redaction_query_credentials import QUERY_BASES, QUERY_CASES, QUERY_TOKENS, _query_targets  # noqa: PLC0415

    for case in QUERY_CASES:
        for token in QUERY_TOKENS.values():
            corpora += [target for _, _, _, target in _query_targets(case, token, keys=("token", "api_key", "sig"))]
    assert len(QUERY_BASES) == 3 and len(corpora) > 16_000
    for text in corpora:
        once = lr.redact(text)
        assert lr.redact(once) == once, (text, once, lr.redact(once))


# ---------------------------------------------------------------------------
# 3. N-1：沒有舊規則會動的行（沒有 token 鍵、沒有 /private_ 前綴），值一路遮到字面的分隔字元
# ---------------------------------------------------------------------------

_PLAIN_NAMES = ["password", "api_key", "Client-Secret", "sig", "X-Amz-Signature", "jwt", "refresh_token", "passphrase"]
_PLAIN_VALUES = [
    f"{SECRET}%2F{T}%20{TAIL}",  # %2F 之後才出現編碼空白（審查者 N-1：舊版遮到 %2F 就停，解碼後的規則又在 %20 停，尾段外洩）
    f"{SECRET}%2F{T}%22{TAIL}",
    f"{SECRET}%5C{T}%C2%A0{TAIL}",
    f"{SECRET}%26{TAIL}",  # 值裡的 %26（密碼含 &）
    f"{SECRET}%3F{T}%20{TAIL}",
    f"{SECRET}%3B{T}%27{TAIL}",
    f"%20{SECRET}%20{T}%2F{TAIL}",  # 反過來的順序原本就有遮
    f"{SECRET}%252F{T}%2520{TAIL}",  # 多一層編碼
    f"%22{SECRET}%2F{TAIL}%22",
]


def _assert_plain_line_masks(name: str, value: str) -> None:
    for text, shown in (
        (f"/x?{name}={value}&limit=5", f"/x?{name}=***&limit=5"),
        (f"/x?a=1&{name}={value}&limit=5", f"/x?a=1&{name}=***&limit=5"),
        (f"GET /x?{name}={value} HTTP/1.1", f"GET /x?{name}=*** HTTP/1.1"),
        (f"/x?a=1;{name}={value}", f"/x?a=1;{name}=***"),
    ):
        assert lr.redact(text) == shown, (text, lr.redact(text))
        assert lr.redact(shown) == shown
        assert TAIL not in lr.redact(text) and SECRET not in lr.redact(text)


@pytest.mark.parametrize("name", _PLAIN_NAMES)
@pytest.mark.parametrize("value", _PLAIN_VALUES)
def test_plain_lines_mask_the_whole_value_through_encoded_delimiters(name, value):
    _assert_plain_line_masks(name, value)


@pytest.mark.parametrize("name", ["token", "access_token", "Access-Token"])
@pytest.mark.parametrize("value", [v for v in _PLAIN_VALUES if v.startswith("%2")])
def test_plain_lines_mask_token_values_that_start_with_an_encoded_delimiter(name, value):
    """token 的值以編碼空白、引號開頭時舊規則比對不到（這一行沒有舊規則會動），整段遮；值本身是 R1 認得的寫法
    （``token=<值>%2F<值>%20<尾>``）有舊規則會動，照 443e95a（已知限制）。"""
    _assert_plain_line_masks(name, value)


def test_plain_lines_do_not_swallow_the_next_parameter():
    assert lr.redact(f"/x?password={SECRET}%2F{T}%20{TAIL}&limit=5&page=2") == "/x?password=***&limit=5&page=2"
    assert lr.redact(f"/x?a=1&limit=5&password={SECRET}%26{TAIL}") == "/x?a=1&limit=5&password=***"
    assert lr.redact("/x?limit=5&page=2&q=%E4%B8%AD") == "/x?limit=5&page=2&q=%E4%B8%AD"  # 沒有憑證：與原本逐字相同


def test_private_key_parameter_name_is_not_a_prefix():
    """``?private_key=`` 的 ``private`` 不是 ``/private_`` 前綴：這一行沒有舊規則會動，照樣用寬的規則（冪等：第一次與第二次的判斷
    要一致，``private_key`` 被別的遮罩吞掉之後第二次看到的就是沒有 ``private`` 的行）。"""
    text = f"/x?private_key=%20{SECRET}%2F{T}%20{TAIL}&q=1"
    assert lr.redact(text) == "/x?private_key=***&q=1"
    both = f"/x?api-key=%29sk-{SECRET}%29;private_key={SECRET}&Access_Token=%27{SECRET}%2F{T}%27&q=1"
    once = lr.redact(both)
    assert SECRET not in once and lr.redact(once) == once


def test_token_key_that_a_later_mask_swallows_does_not_change_the_second_pass():
    """有 token 鍵的行用比較保守的規則；token 鍵被別的遮罩吞掉之後輸出變成「沒有舊規則會動的行」，第一次就要把第二輪做完。"""
    text = f"/key/info?API_KEY={SECRET}=?token=%28{T}&token=%22{SECRET}/{T}%22 done"
    once = lr.redact(text)
    assert SECRET not in once and T not in once, once
    assert lr.redact(once) == once


def test_token_value_that_starts_with_a_delimiter_is_masked_through_the_next_delimiter_in_plain_lines():
    # 舊的 token 規則比對不到的寫法：值以編碼空白開頭、或以 ``;`` 開頭（``access-token`` 的連字號 R1 也不認）
    assert lr.redact(f"/x?token=%20{SECRET}%2F{T}&tail=1") == "/x?token=***&tail=1"
    assert lr.redact(f"/x?access-token=%3B{SECRET}") == "/x?access-token=***"
    assert lr.redact(f"/x?acc%%365ss-token=%3B`{SECRET}") == "/x?access-token=***"  # 鍵名只有解碼後才認得


# ---------------------------------------------------------------------------
# 4. N-2：截斷點不切開最後一個參數（第二次遮罩看到的才與第一次相同）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("unit", ["?token=%20a/%2570%2572%2569vate_x", "?token=abc /%2570rivate_x", "/%252570rivate_x?token=a ",
                                  "/p%2572ivate_x", "?sig=a/b /p%2572ivate_x"])
def test_truncation_does_not_cut_the_last_parameter_in_half(unit):
    """repro.py F4（審查者 N-2）：舊版與 c3443a5 在這 5 種單位各 200 種對齊中有 0～45 種第二次遮罩的結果不同——新版保留原本拼法、
    輸出比 443e95a 長，切點常落在 ``?tok``（第二次是「沒有 = 的參數」，整段 query 遮成 ``?***``）或 ``?token=%20a``（切掉後面的
    ``/…`` 之後整段乾淨，第二次才遮）。"""
    limit = lr.MAX_REDACT_CHARS
    for pad in range(0, 40, 3):
        for target in (limit - 8, limit - 3, limit, limit + 3, limit + 40):
            text = ("x" * pad + unit * (target // len(unit) + 2))[:target]
            once = lr.redact(text)
            assert lr.redact(once) == once, (unit, pad, target, once[-60:])
            assert len(once) <= limit + 64


def test_dangling_param_helper_backs_off_to_the_last_lead():
    assert lr._dangling_param("abc?tok", 7) == 4
    assert lr._dangling_param("abc?token=%20a", 14) == 11
    assert lr._dangling_param("abc?a=1&b=2", 11) == 4
    assert lr._dangling_param("abc?a=1&b=2 ", 12) == 0  # 結尾是空白：切點在參數之間
    assert lr._dangling_param("x" * 200, 200) == 0  # 128 字元內沒有 lead
    assert lr._safe_cut("abc?a=1&b=2", 11) == 7


# ---------------------------------------------------------------------------
# 5. N-5：名單與一般參數
# ---------------------------------------------------------------------------

NEW_NAMES = ("refresh_token", "id_token", "auth_token", "session_token", "app_key", "consumer_secret", "passphrase")


@pytest.mark.parametrize("name", NEW_NAMES)
def test_oauth_session_and_app_key_names_are_credentials(name):
    for variant in (name, name.upper(), name.replace("_", "-"), name.replace("_", ""), name.title()):
        assert lr.redact(f"/x?{variant}={SECRET}&limit=5") == f"/x?{variant}=***&limit=5"
        assert lr.redact(f"/x?a=1&{variant}=%20{SECRET}") == f"/x?a=1&{variant}=***"
    if "_" in name:
        spelled = name.replace("_", "%5F")
        assert lr.redact(f"/x?{spelled}={SECRET}") == f"/x?{spelled}=***"
    assert lr.redact("/x?limit=5") == "/x?limit=5"


@pytest.mark.parametrize("name", ["tokens", "token_type", "mytoken", "refresh_tokens", "id_tokens", "auth_tokens", "session_tokens",
                                  "app_keys", "app_key_id", "consumer_secrets", "passphrases", "passphrase_hint", "xrefresh_token",
                                  "session_token_id", "idtoken_type", "auth_tok", "refresh"])
def test_only_complete_names_count(name):
    """只認完整名稱：前綴、後綴、複數、子字串都不算（``session_token`` 原本也在這份清單裡，N-5 起改成憑證鍵名，改用別的名字釘）。"""
    text = f"/x?{name}=abc&limit=5"
    assert lr.redact(text) == text


def test_empty_value_followed_by_a_bare_param_is_masked_as_the_pasted_token():
    """``_EMPTY_THEN_BARE_RE``：值是空的、後面接著沒有 ``=`` 的參數時把那個參數遮掉（``?token=%26<JWT>`` 在有 token 鍵或前綴的行，
    解碼後是 ``?token=&<JWT>``）。代價（審查者 N-5，輕微過度遮罩、已寫進文件）：``?auth=&debug`` 的 ``debug`` 也遮；解碼後的
    字串分不出 ``%26`` 與字面 ``&``。"""
    assert lr.redact(f"/x?token=%26{SECRET}") == "/x?token=***"  # 沒有舊規則會動的行：``%26`` 是值的一部分，整段遮
    assert lr.redact(f"/x?token=%26{SECRET}&password=%26{SECRET}") == "/x?token=***&password=***"
    assert lr.redact("/x?auth=&debug") == "/x?auth=&***"
    assert lr.redact("/x?auth=&debug&q=1") == "/x?auth=&***&q=1"
    assert lr.redact("/x?auth=1&debug") == "/x?auth=***&debug"


def test_install_compiles_every_lazy_pattern_before_the_first_request():
    lr._warm_patterns()
    assert set(lr._clean_value_res) == {("wide", "all"), ("lenient", "all"), ("lenient", "q"), ("lenient", "amp"),
                                        ("strict", "qs"), ("strict", "q")}


# ---------------------------------------------------------------------------
# 6. N-4：變異測試存活的規則（審查者的 52 個變異、18 個存活；每條存活的規則一個斷言，再加我們自己的變異）
# ---------------------------------------------------------------------------

def test_fragment_with_ampersand_or_semicolon_and_equals_is_a_candidate():
    """``_has_candidate`` 的「有 ``=`` 且有 ``&`` 或 ``;``」條款：沒有 ``?``、``%``、``token=``、``private`` 的片段（``&api_key=…``、
    矩陣參數 ``/x;password=…``）也要進到鍵名規則（CLAUDE.md 寫了這個行為，原本沒有任何測試）。"""
    assert lr.redact(f"x&password={SECRET}") == "x&password=***"
    assert lr.redact(f"/m;secret={SECRET}") == "/m;secret=***"
    assert lr.redact(f"a=1&api_key={SECRET}&b=2") == "a=1&api_key=***&b=2"
    assert lr.redact("a=1&b=2") == "a=1&b=2" and lr.redact("a&b;c") == "a&b;c"
    assert lr._has_candidate("x&password=1") and lr._has_candidate("/m;secret=1")
    assert not lr._has_candidate("a&b;c") and not lr._has_candidate("a=1")


_ENCODED_NAME_FORMS = [
    "api_k%65y", "%6bey", "%4Bey", "%2561pi_key",  # 編碼的鍵名字母（含第一個字）
    "api%5Fkey", "api%2Dkey", "x%2Dapi%2Dkey", "api%255Fkey",  # 編碼的 ``_``、``-``
    "jw%74", "si%67", "pw%64", "%6A%77t", "%73ig",  # 以編碼的十六進位數字結尾（``_CLEAN_HINT_RE``）、第一個字是編碼
    "toKen", "acceſſ_token", "TOKEN",  # token 的 Kelvin／長 s 折疊（只有 token 認折疊）
]


@pytest.mark.parametrize("name", _ENCODED_NAME_FORMS)
@pytest.mark.parametrize("base", ["/x", "/private_x/mcp", "/x?token=abc&limit=5"])
def test_encoded_name_forms_with_a_value_that_starts_with_an_encoded_space_are_masked(name, base):
    """值以編碼空白開頭，解碼後的規則比對不到（``key= <值>``），只有拼法階段的規則遮得到：鍵名的每一種編碼寫法都要認得。
    三種行（沒有舊規則會動、有 ``/private_`` 前綴、有 token 鍵）用的是三個不同的 pattern。"""
    joiner = "&" if "?" in base else "?"
    text = f"{base}{joiner}{name}=%20{SECRET}"
    out = lr.redact(text)
    assert SECRET[:8] not in out and SECRET[8:] not in out, (text, out)
    assert lr.redact(out) == out


@pytest.mark.parametrize("lead", ["%26", "%3B", "%3F", "%2526", "%253B", "%253F"])
@pytest.mark.parametrize("base", ["/x?u=1", "/private_x/mcp?u=1", "/x?token=abc&u=1"])
def test_encoded_leads_with_a_value_that_starts_with_an_encoded_space_are_masked(lead, base):
    """lead（``& ; ?``）本身被編碼（網址被當成另一個參數的值時常見）：``?u=1%26key=%20<值>``。"""
    out = lr.redact(f"{base}{lead}key=%20{SECRET}")
    assert SECRET[:8] not in out and SECRET[8:] not in out, out


def test_encoded_equals_with_a_value_that_starts_with_an_encoded_space_is_masked():
    for base in ("/x", "/private_x/mcp", "/x?token=abc&limit=5"):
        joiner = "&" if "?" in base else "?"
        for eq in ("%3D", "%253D"):
            out = lr.redact(f"{base}{joiner}key{eq}%20{SECRET}")
            assert SECRET[:8] not in out and SECRET[8:] not in out, out


def test_name_that_is_only_recognised_after_decoding_masks_a_value_that_starts_with_x():
    """十六進位數字本身也編碼的鍵名（``ap%%36%39_key``）只有解碼後的規則認得；``_NAMED_HINT_RE`` 的必要條件不可擋掉任何值。"""
    for value in (f"x{SECRET}", f"abc{SECRET}", f"9{SECRET}", f"_{SECRET}"):
        out = lr.redact(f"/x?ap%%36%39_key={value}&limit=5")
        assert value not in out and out.endswith("&limit=5"), out


# 乾淨值的規則（log_redaction 第 1 點）：值先跳過開頭的字面引號，之後是「乾淨」的字元，遇到下面任何一個就停在那裡（整段乾淨才遮的
# token 與 ``?``／``;`` lead 的非 token 鍵名在這種值上整個不遮）。直接測拼法階段的函式：端到端的輸出會被解碼後的規則蓋過去。
_UNCLEAN_STOPS = ["/", "\\", "?", ";", "*", "ï¿½".encode("latin-1").decode("utf-8"),  # 字面
                  "%2F", "%2f", "%5C", "%5c", "%3F", "%3b", "%26", "%23", "%252F", "%255C",  # 解碼後是 / \ ? ; & # 的完整跳脫（含多一層編碼）
                  "%zz", "%", "%2", "%%32F"]  # 不完整或無效的跳脫


@pytest.mark.parametrize("stop", _UNCLEAN_STOPS)
def test_lenient_clean_prefix_stops_at_every_unclean_character(stop):
    assert lr._mask_clean_values(f"?api_key=%20ab{stop}xy%20TAIL", "lenient") == f"?api_key=***{stop}xy%20TAIL"
    assert lr._mask_clean_values(f"&password='ab{stop}xy", "lenient") == f"&password=***{stop}xy"


@pytest.mark.parametrize("stop", _UNCLEAN_STOPS)
def test_strict_and_token_values_need_a_whole_clean_value(stop):
    """有 token 鍵時 ``?``／``;`` lead 的非 token 鍵名、以及任何一行的 token：值不整段乾淨就整個不在拼法階段遮。"""
    for text in (f"?api_key=%20ab{stop}xy%20TAIL", f";sig=%22ab{stop}xy&x=1"):
        assert lr._mask_clean_values(text, "strict") == text
    for text in (f"?token=%20ab{stop}xy&x=1", f"&access_token='ab{stop}xy"):
        assert lr._mask_clean_values(text, "lenient") == text
        assert lr._mask_clean_values(text, "strict") == text


def test_clean_value_rules_in_detail():
    """整段乾淨的值一路到字面的 ``& # 空白 " '`` 或結尾；``&`` lead 的非 token 鍵名在 strict 模式照舊只要乾淨的開頭。"""
    for tail in ("", "&x=1", "#frag", " HTTP/1.1", "'", '"'):
        assert lr._mask_clean_values(f"?api_key=%20abc%2Bdef{tail}", "strict") == f"?api_key=***{tail}"
        assert lr._mask_clean_values(f"?token='%20abc'{tail}", "lenient") == f"?token=***'{tail}"
    assert lr._mask_clean_values("&api_key=%20ab/cd", "strict") == "&api_key=***/cd"
    assert lr._mask_clean_values("%26api_key=%20ab/cd", "strict") == "%26api_key=***/cd"
    assert lr._mask_clean_values("?api_key=%20ab/cd", "lenient", amp=False) == "?api_key=***/cd"
    assert lr._mask_clean_values("&api_key=%20ab/cd", "lenient", amp=False) == "&api_key=%20ab/cd"  # ``& ;`` lead 不遮（解碼後才認得的前綴）
    assert lr._mask_clean_values("?api_key=***&x=1", "lenient") == "?api_key=***&x=1"  # 已遮的不再動


def test_wide_value_runs_to_the_next_literal_delimiter():
    for stop in _UNCLEAN_STOPS:
        assert lr._mask_clean_values(f"?api_key=%20ab{stop}xy%20TAIL&x=1", "wide") == "?api_key=***&x=1"
        assert lr._mask_clean_values(f"?token=ab{stop}xy%22TAIL#frag", "wide") == "?token=***#frag"
    assert lr._mask_clean_values("?password='abc'&x=1", "wide") == "?password=***'&x=1"  # 開頭的字面引號跳過，結尾的留著
    assert lr._mask_clean_values("?password=&x=1", "wide") == "?password=&x=1"  # 空值：沒有東西可遮


# ---------------------------------------------------------------------------
# 7. 線性：寬鬆的前綴判斷（``_has_prefix``，每一行有 ``private`` 字樣都會呼叫）
# ---------------------------------------------------------------------------

_NOISE_SHAPES = {
    "/private+backslash": lambda n: "/private" + "\\" * n,
    "/private+ufffd": lambda n: "/private" + "�" * n,
    "x/p+noise+rivate_": lambda n: "x/p" + "\\�" * (n // 2) + "rivate_",
    "backslash runs + p": lambda n: ("\\" * 50 + "p") * (n // 51),
    "\\private repeated": lambda n: "\\private" * (n // 8),
    "ufffd/private repeated": lambda n: "�/private" * (n // 10),
}


@pytest.mark.parametrize("name", list(_NOISE_SHAPES))
def test_prefix_detection_is_linear_on_noise_heavy_input(name):
    """``_LOOSE_PREFIX_RE`` 第一版沒有「噪音連續段只從開頭起算」：``/private`` 接 64 K 個反斜線每個位置各掃一次，O(n²)，實測 40 秒
    （既有的 test_log_redaction_linear.py 抓到的）。時間用輸入加倍的比值與寬的絕對上限量，不吃機器負載。"""
    from test_log_redaction_query_credentials import _best_pair  # noqa: PLC0415

    make = _NOISE_SHAPES[name]
    small_text, big_text = make(32_768), make(65_536)

    def work(text):
        return lr.redact(text), lr._has_prefix(text, text.lower())

    start = time.process_time()
    work(big_text)
    first = time.process_time() - start
    assert first < 1.0, f"{name}: {first:.1f} s for 64 KiB"  # 線性約 10 ms，平方 40 秒：先量一次，退步時不必等完整的量測
    small = big = 0.0
    for _ in range(3):
        small, big = _best_pair(small_text, big_text, work)
        assert big < 0.5, f"{name}: {big * 1000:.1f} ms for 64 KiB"
        if small <= 0.002 or big / small < 2.8:  # 線性約 2，平方 4（與 test_log_redaction_query_credentials.DOUBLING_LIMIT 相同）
            return
    pytest.fail(f"{name}: {small * 1000:.2f} ms -> {big * 1000:.2f} ms for 2x input")


@pytest.mark.parametrize("text", **_numbered("prefixq", [
    f"/p%5Crivate_x?token=%20{SECRET}",
    f"/p%5Crivate_x?api_key=%20{SECRET}&limit=5",
    f"/p%5Crivate_x%3Ftoken=%20{SECRET}",
    f"/p%5Crivate_x%3Fapi_key=%20{SECRET}&limit=5",
    f"/pr%6%39vate_x%3Ftoken%3D%20{SECRET}",
    f"/%2570rivate_x?sig=%22{SECRET}%22",
]))
def test_decoded_only_prefix_still_masks_question_mark_led_values_that_start_with_an_encoded_space(text):
    """解碼後才認得的前綴（``p%5Crivate_``）的行：lead 是 ``?``（含編碼的 ``%3F``）的值照樣在拼法階段遮；只有第一個前綴與後面另一個
    前綴之間夾著遮掉的值時才不遮（``test_two_decoded_only_prefixes_with_a_masked_value_between_them_stay_masked``）。"""
    out = lr.redact(text)
    assert SECRET[:8] not in out and SECRET[8:] not in out, out
    assert lr.redact(out) == out
