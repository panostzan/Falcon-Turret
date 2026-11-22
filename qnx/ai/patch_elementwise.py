"""Compatibility patch for the Pi's compiler: kernels/elementwise.cc passes std::abs<T> as a function, which newer libc++ rejects.
Wraps both uses in a tiny lambda instead. Safe to run twice."""
import os
import sys

p = os.path.expanduser("~/ai/tensorflow/tensorflow/lite/kernels/elementwise.cc")
s = open(p).read()
fixes = [("EvalImpl<float>(context, node, std::abs<float>, type)", "EvalImpl<float>(context, node, [](float v) { return std::fabs(v); }, type)"),
         ("EvalImpl<int32_t>(context, node, std::abs<int32_t>, type)", "EvalImpl<int32_t>(context, node, [](int32_t v) { return v < 0 ? -v : v; }, type)")]
done = 0
for old, new in fixes:
    if old in s:
        s = s.replace(old, new)
        done += 1
open(p, "w").write(s)
print("patched %d of 2 places (0 means it was already patched)" % done)
