#include "../tip_json.h"
#include <cassert>
#include <climits>

using tip_json::Value;
static Value Parse(const std::string& s) {
    Value v;
    assert(tip_json::Parser(s).Parse(v));
    return v;
}
int main() {
    // Endpoint fields are looked up on the object, never inside another string.
    auto endpoint = Parse(R"({"version":1,"port":12345,"token":"test-only"})");
    long long n = 0;
    assert(endpoint.Get("port").Int(n) && n == 12345);
    assert(endpoint.Get("token").text == "test-only");
    auto fake = Parse(R"({"nested":{"ok":true},"msg":"\"ok\":true"})");
    assert(fake.Get("ok").kind == Value::Null);
    assert(fake.Get("nested").Get("ok").boolean);

    // json.dumps ensure_ascii=True/False must decode to the same UTF-8 text.
    auto escaped = Parse(R"({"s":"\u3068\ud83d\ude00\"\\\n\r\t\b\f\/\u0000"})");
    std::string expected = "\xe3\x81\xa8\xf0\x9f\x98\x80\"\\\n\r\t\b\f/";
    expected += '\0';
    assert(escaped.Get("s").text == expected);
    assert(Parse(tip_json::Quote(expected)).text == expected);
    assert(Parse("\"\xe3\x81\xa8\xf0\x9f\x98\x80\"").text ==
           Parse(R"("\u3068\ud83d\ude00")").text);

    auto reply = Parse(R"({"direct":false,"pass_through":false,"composition":"\u3068",
      "commit":"","converted":true,"n_tokens":1,"timer":null,"candidates":["a\"b","c\\d"],
      "segments":[{"display_start":0,"display_end":1}],"focus":0,"candidate_page":2,
      "has_next_page":true,"cand_index":0})");
    assert(reply.Get("timer").kind == Value::Null);
    assert(!reply.Get("direct").boolean && reply.Get("has_next_page").boolean);
    assert(reply.Get("segments").array[0].Get("display_end").Int(n) && n == 1);
    assert(reply.Get("candidates").array[0].text == "a\"b");
    // "pending" (LM wait) is optional: absent keeps the default, a bool is read,
    // any other type rejects the reply.
    bool pending = true;
    assert(tip_json::OptionalBool(reply, "pending", pending) && pending);
    assert(tip_json::OptionalBool(Parse(R"({"pending":false})"), "pending", pending) && !pending);
    assert(tip_json::OptionalBool(Parse(R"({"pending":true})"), "pending", pending) && pending);
    assert(!tip_json::OptionalBool(Parse(R"({"pending":1})"), "pending", pending));
    assert(!tip_json::OptionalBool(Parse(R"({"pending":null})"), "pending", pending));
    assert(Parse(" [true,false,null,-12,0.5,1e-2] \r\n").array.size() == 6);
    assert(!Parse("1e2").Int(n));
    assert(Parse("9223372036854775807").Int(n) && n == LLONG_MAX);
    assert(Parse("-9223372036854775808").Int(n) && n == LLONG_MIN);
    assert(!Parse("9223372036854775808").Int(n));
    assert(!Parse("-9223372036854775809").Int(n));
    assert(!Parse("999999999999999999999999999999999999").Int(n));

    const char* invalid[] = {
        "", "{", "[] trailing", "truex", "01", "-", "1.", "1e+", "[1,]", "{\"a\":1,}",
        "{\"a\":1,\"a\":2}", "\"\\q\"", "\"\\u12\"", "\"\\ud800\"", "\"\\udc00\"",
        "\"\\ud800\\u0041\"", "\"raw\nnewline\"", "\"unterminated"
    };
    for (const char* s : invalid) {
        Value v;
        assert(!tip_json::Parser(s).Parse(v));
    }
    return 0;
}
