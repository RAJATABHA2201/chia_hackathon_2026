module tb_sparse_pe;

    // ------------------------------------------------
    // Parameters
    // ------------------------------------------------

    parameter NUM_GROUPS = 4;


    // ------------------------------------------------
    // Signals
    // ------------------------------------------------

    logic clk;
    logic rst;
    logic start;

    logic signed [7:0] value0 [NUM_GROUPS];
    logic signed [7:0] value1 [NUM_GROUPS];

    logic [1:0] index0 [NUM_GROUPS];
    logic [1:0] index1 [NUM_GROUPS];

    logic signed [7:0] b0 [NUM_GROUPS];
    logic signed [7:0] b1 [NUM_GROUPS];
    logic signed [7:0] b2 [NUM_GROUPS];
    logic signed [7:0] b3 [NUM_GROUPS];

    logic signed [31:0] result;
    logic done;


    // ------------------------------------------------
    // Instantiate Sparse PE
    // ------------------------------------------------

    sparse_pe #(
        .DATA_WIDTH(8),
        .OUT_WIDTH(32),
        .NUM_GROUPS(NUM_GROUPS)
    ) dut (

        .clk(clk),
        .rst(rst),
        .start(start),

        .value0(value0),
        .value1(value1),

        .index0(index0),
        .index1(index1),

        .b0(b0),
        .b1(b1),
        .b2(b2),
        .b3(b3),

        .result(result),
        .done(done)
    );


    // ------------------------------------------------
    // Clock
    // ------------------------------------------------

    always #5 clk = ~clk;


    // ------------------------------------------------
    // Test
    // ------------------------------------------------

    initial begin

        integer i;

        clk = 0;
        rst = 1;
        start = 0;


        // Initialize all groups
        for (i = 0; i < NUM_GROUPS; i = i + 1) begin

            value0[i] = 0;
            value1[i] = 0;

            index0[i] = 0;
            index1[i] = 0;

            b0[i] = 0;
            b1[i] = 0;
            b2[i] = 0;
            b3[i] = 0;

        end


        // ------------------------------------------------
        // Reset
        // ------------------------------------------------

        #10;
        rst = 0;


        // ------------------------------------------------
        // GROUP 0
        //
        // A = [3, 0, 0, 7]
        // B = [10, 20, 30, 40]
        //
        // Result = 3*10 + 7*40
        //        = 310
        // ------------------------------------------------

        value0[0] = 3;
        value1[0] = 7;

        index0[0] = 0;
        index1[0] = 3;

        b0[0] = 10;
        b1[0] = 20;
        b2[0] = 30;
        b3[0] = 40;


        // ------------------------------------------------
        // GROUP 1
        //
        // A = [2, 5, 0, 0]
        // B = [1, 2, 3, 4]
        //
        // Result = 2*1 + 5*2
        //        = 12
        // ------------------------------------------------

        value0[1] = 2;
        value1[1] = 5;

        index0[1] = 0;
        index1[1] = 1;

        b0[1] = 1;
        b1[1] = 2;
        b2[1] = 3;
        b3[1] = 4;


        // ------------------------------------------------
        // GROUP 2
        //
        // A = [6, 0, 8, 0]
        // B = [2, 3, 4, 5]
        //
        // Result = 6*2 + 8*4
        //        = 44
        // ------------------------------------------------

        value0[2] = 6;
        value1[2] = 8;

        index0[2] = 0;
        index1[2] = 2;

        b0[2] = 2;
        b1[2] = 3;
        b2[2] = 4;
        b3[2] = 5;


        // ------------------------------------------------
        // GROUP 3
        //
        // A = [0, 9, 0, 4]
        // B = [1, 2, 3, 4]
        //
        // Result = 9*2 + 4*4
        //        = 34
        // ------------------------------------------------

        value0[3] = 9;
        value1[3] = 4;

        index0[3] = 1;
        index1[3] = 3;

        b0[3] = 1;
        b1[3] = 2;
        b2[3] = 3;
        b3[3] = 4;


        // ------------------------------------------------
        // Total expected result
        //
        // 310 + 12 + 44 + 34 = 400
        // ------------------------------------------------

        #5;
        start = 1;

        #10;
        start = 0;

        #5;


        // ------------------------------------------------
        // Display results
        // ------------------------------------------------

        $display("");
        $display("========================================");
        $display("       SparseCraft Sparse PE");
        $display("========================================");

        $display("Number of groups = %0d", NUM_GROUPS);

        $display("----------------------------------------");

        $display("Group 0 result = 310");
        $display("Group 1 result = 12");
        $display("Group 2 result = 44");
        $display("Group 3 result = 34");

        $display("----------------------------------------");

        $display("PE Result       = %0d", result);
        $display("Expected        = 400");

        $display("----------------------------------------");


        // ------------------------------------------------
        // Correctness check
        // ------------------------------------------------

        if (result == 400)
            $display("STATUS          = PASS");
        else
            $display("STATUS          = FAIL");


        $display("========================================");
        $display("");


        #10;

        $finish;

    end

endmodule