/* Confined to an ephemeral network=none target container; no host files touched. */
#include <stdio.h>
#include <string.h>
#ifndef _WIN32
#include <sys/types.h>
#include <unistd.h>
#endif
int main(int argc, char **argv) {
    if (argc != 2) return 2;
    if (!strcmp(argv[1], "flood")) {
        for (;;) { fputs("0123456789abcdef0123456789abcdef\n", stdout); fflush(stdout); }
    }
#ifndef _WIN32
    if (!strcmp(argv[1], "child")) {
        pid_t child = fork();
        if (child == 0) for (;;) pause();
        printf("child-created\n"); fflush(stdout);
        for (;;) pause();
    }
#endif
    if (!strcmp(argv[1], "timeout")) for (;;) { volatile unsigned n = 0; ++n; (void)n; }
    return 2;
}
