#include <cstdint>
#include <cstdio>
#include <llvm-c/Analysis.h>
#include <llvm-c/Core.h>
#include <llvm-c/LLJIT.h>
#include <llvm-c/Target.h>
#include <llvm-c/TargetMachine.h>
#include <llvm-c/Transforms/PassBuilder.h>

static bool failed(LLVMErrorRef error) {
  if (!error)
    return false;
  char *message = LLVMGetErrorMessage(error);
  std::fprintf(stderr, "%s\n", message);
  LLVMDisposeErrorMessage(message);
  return true;
}
#ifdef _WIN32
#define SDK_EXPORT __declspec(dllexport)
#else
#define SDK_EXPORT __attribute__((visibility("default")))
#endif
extern "C" SDK_EXPORT int sdk_evaluate(int input, int *output) {
  static const bool initialization_failed =
      LLVMInitializeNativeTarget() || LLVMInitializeNativeAsmPrinter();
  if (initialization_failed)
    return 1;
  LLVMOrcLLJITRef jit = nullptr;
  if (failed(LLVMOrcCreateLLJIT(&jit, nullptr)))
    return 2;
  LLVMContextRef context = LLVMContextCreate();
  auto tsc = LLVMOrcCreateNewThreadSafeContextFromLLVMContext(context);
  auto module = LLVMModuleCreateWithNameInContext("sdk-smoke", context);
  LLVMSetTarget(module, LLVMOrcLLJITGetTripleString(jit));
  LLVMSetDataLayout(module, LLVMOrcLLJITGetDataLayoutStr(jit));
  auto i32 = LLVMInt32TypeInContext(context);
  auto function =
      LLVMAddFunction(module, "evaluate", LLVMFunctionType(i32, &i32, 1, 0));
  auto builder = LLVMCreateBuilderInContext(context);
  LLVMPositionBuilderAtEnd(
      builder, LLVMAppendBasicBlockInContext(context, function, "entry"));
  auto product = LLVMBuildMul(builder, LLVMGetParam(function, 0),
                              LLVMConstInt(i32, 3, 0), "");
  LLVMBuildRet(builder,
               LLVMBuildAdd(builder, product, LLVMConstInt(i32, 7, 0), ""));
  LLVMDisposeBuilder(builder);
  char *message = nullptr;
  if (LLVMVerifyModule(module, LLVMReturnStatusAction, &message)) {
    std::fprintf(stderr, "%s\n", message);
    LLVMDisposeMessage(message);
    return 3;
  }
  LLVMDisposeMessage(message);
  LLVMTargetRef target = nullptr;
  if (LLVMGetTargetFromTriple(LLVMOrcLLJITGetTripleString(jit), &target,
                              &message))
    return 4;
  auto machine = LLVMCreateTargetMachine(
      target, LLVMOrcLLJITGetTripleString(jit), "generic", "",
      LLVMCodeGenLevelDefault, LLVMRelocDefault, LLVMCodeModelJITDefault);
  if (!machine)
    return 4;
  auto options = LLVMCreatePassBuilderOptions();
  bool bad = failed(LLVMRunPasses(module, "default<O2>", machine, options));
  LLVMDisposePassBuilderOptions(options);
  LLVMDisposeTargetMachine(machine);
  if (bad)
    return 5;
  auto tsm = LLVMOrcCreateNewThreadSafeModule(module, tsc);
  LLVMOrcDisposeThreadSafeContext(tsc);
  if (failed(LLVMOrcLLJITAddLLVMIRModule(jit, LLVMOrcLLJITGetMainJITDylib(jit),
                                         tsm)))
    return 6;
  LLVMOrcExecutorAddress address = 0;
  if (failed(LLVMOrcLLJITLookup(jit, &address, "evaluate")))
    return 7;
  *output =
      reinterpret_cast<int (*)(int)>(static_cast<uintptr_t>(address))(input);
  return failed(LLVMOrcDisposeLLJIT(jit)) ? 8 : 0;
}
