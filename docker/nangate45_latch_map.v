// Yosys techmap rules for NanGate45 D-latches.
//
// hammer's yosys plugin emits `techmap -map <synthesis.yosys.latch_map_file>`
// unconditionally, because its own defaults.yml DECLARES that key as null and
// the plugin tests `has_setting` rather than the value. Any technology whose
// plugin does not override it therefore gets the literal command
// `techmap -map None`, and yosys stops with "Can't open input file `None'".
// asap7 ships such a file; nangate45 does not. This is it.
//
// Cell choice comes from the NanGate45 Liberty, not from guesswork: DLH_X1
// declares `enable: "G"` active high, DLL_X1 is its active-low counterpart,
// and both expose exactly D / G / Q.
module \$_DLATCH_P_ (input E, input D, output Q);
    DLH_X1 _TECHMAP_REPLACE_ (
        .D(D),
        .G(E),
        .Q(Q)
    );
endmodule

module \$_DLATCH_N_ (input E, input D, output Q);
    DLL_X1 _TECHMAP_REPLACE_ (
        .D(D),
        .G(E),
        .Q(Q)
    );
endmodule
