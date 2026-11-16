/* Over 100 noinline C++ function instantiations; bounded defined arithmetic. */
#include <cstdio>
template<int N> __attribute__((noinline)) long long step(long long value) {
    return step<N - 1>(value) + N;
}
template<> __attribute__((noinline)) long long step<0>(long long value) {
    return value;
}
int main() {
    long long value;
    if (std::scanf("%lld", &value) != 1 || value < -10000 || value > 10000) {
        std::fputs("invalid integer\n", stderr); return 2;
    }
    std::printf("%lld\n", step<128>(value));
    return 0;
}
