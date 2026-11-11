/* Debug-format fixture: two functions, each with a distinct line range. */
int fixture_helper(int value) {
    return value + 3;
}

int fixture_entry(int value) {
    int scaled = fixture_helper(value) * 2;
    return scaled;
}
