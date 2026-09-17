#include <cstdio>
extern "C" int sdk_evaluate(int input, int *output);
int main() {
  const int inputs[] = {-7, 0, 9};
  for (int input : inputs) {
    int output = 0;
    if (sdk_evaluate(input, &output) != 0 || output != input * 3 + 7) {
      std::fprintf(stderr, "JIT failed for %d: %d\n", input, output);
      return 1;
    }
  }
  std::puts("Static LLVM JIT from shared library passed");
  return 0;
}
