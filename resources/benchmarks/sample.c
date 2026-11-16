/* Recheck-owned static-analysis probe. Contains no filesystem or network access. */
#include <stdio.h>
#include <stdlib.h>

static int bounded_sum(int count) {
    int total = 0;
    for (int i = 0; i < count && i < 100; ++i) {
        total += (i % 2) ? i * 2 : i;
    }
    return total;
}

int main(int argc, char **argv) {
    const int count = argc > 1 ? atoi(argv[1]) : 8;
    printf("recheck_probe=%d\n", bounded_sum(count));
    return 0;
}
